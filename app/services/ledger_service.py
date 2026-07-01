"""
LedgerService — the domain layer.

All invariant enforcement happens here:
  1. Double-entry: every transaction nets to zero.
  2. Overdraft protection: balance - debit >= -overdraft_limit.
  3. Atomic commits: either ALL legs commit or NONE do.
  4. Optimistic concurrency via unique (account_id, sequence) constraint.
  5. Account status checks: no transactions on CLOSED/FROZEN accounts.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    AccountNotFoundError, AccountClosedError, InsufficientFundsError,
    DoubleEntryViolationError, CurrencyMismatchError, NegativeAmountError,
    TransactionAmountError,
)
from app.models.ledger import (
    Account, AccountSnapshot, AccountStatus, AccountType,
    EntryType, LedgerEntry, Transaction, TransactionStatus,
)
from app.repositories.account_repository import AccountRepository
from app.repositories.transaction_repository import TransactionRepository
from app.schemas.ledger import (
    AccountCreateRequest, ManualJournalRequest, TransferRequest,
    AuditTrailEntry, AuditTrailResponse, BalanceResponse,
    PointInTimeBalanceResponse, SnapshotResponse,
)

settings = get_settings()


class LedgerService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.account_repo = AccountRepository(db)
        self.tx_repo = TransactionRepository(db)

    # -----------------------------------------------------------------------
    # Account management
    # -----------------------------------------------------------------------

    async def create_account(
        self, req: AccountCreateRequest, owner_id: str
    ) -> Account:
        account = Account(
            owner_id=owner_id,
            name=req.name,
            account_type=req.account_type,
            currency=req.currency.upper(),
            overdraft_limit=req.overdraft_limit,
        )
        account = await self.account_repo.create(account)
        await self.db.commit()
        await self.db.refresh(account)
        return account

    async def get_account(self, account_id: str) -> Account:
        acc = await self.account_repo.get_by_id(account_id)
        if not acc:
            raise AccountNotFoundError(account_id)
        return acc

    async def list_accounts(self, owner_id: str) -> List[Account]:
        return await self.account_repo.get_by_owner(owner_id)

    async def close_account(self, account_id: str) -> Account:
        acc = await self.get_account(account_id)
        await self.account_repo.update_status(account_id, AccountStatus.CLOSED)
        await self.db.commit()
        await self.db.refresh(acc)
        return acc

    # -----------------------------------------------------------------------
    # Balance queries
    # -----------------------------------------------------------------------

    async def get_balance(self, account_id: str) -> BalanceResponse:
        acc = await self.get_account(account_id)

        # Try snapshot-accelerated path first
        snapshot = await self.account_repo.get_latest_snapshot(account_id)
        if snapshot:
            balance = await self.account_repo.compute_balance_from_snapshot(account_id, snapshot)
            return BalanceResponse(
                account_id=account_id,
                currency=acc.currency,
                balance=balance,
                computed_at=datetime.now(timezone.utc),
                from_snapshot=True,
                snapshot_sequence=snapshot.at_sequence,
            )

        balance = await self.account_repo.compute_balance(account_id)
        return BalanceResponse(
            account_id=account_id,
            currency=acc.currency,
            balance=balance,
            computed_at=datetime.now(timezone.utc),
        )

    async def get_balance_at(
        self, account_id: str, as_of: datetime
    ) -> PointInTimeBalanceResponse:
        acc = await self.get_account(account_id)
        balance, count = await self.account_repo.compute_balance_at(account_id, as_of)
        return PointInTimeBalanceResponse(
            account_id=account_id,
            currency=acc.currency,
            balance=balance,
            as_of=as_of,
            entry_count=count,
        )

    # -----------------------------------------------------------------------
    # Transfer (simple two-leg)
    # -----------------------------------------------------------------------

    async def transfer(
        self, req: TransferRequest, initiated_by: str
    ) -> Transaction:
        # 1. Load and validate both accounts
        from_acc = await self._load_active_account(req.from_account_id)
        to_acc = await self._load_active_account(req.to_account_id)

        self._assert_currency(from_acc, req.currency)
        self._assert_currency(to_acc, req.currency)
        self._assert_amount(req.amount)

        # 2. Check from_acc has sufficient funds
        from_balance = await self._get_raw_balance(req.from_account_id, from_acc)
        available = from_balance + from_acc.overdraft_limit
        if available < req.amount:
            raise InsufficientFundsError(
                req.from_account_id, float(available), float(req.amount)
            )

        # 3. Build transaction and two immutable ledger entries
        reference = req.reference or str(uuid.uuid4())
        tx = Transaction(
            reference=reference,
            description=req.description,
            status=TransactionStatus.COMMITTED,
            created_by=initiated_by,
        )
        tx = await self.tx_repo.create(tx)

        from_seq = await self.account_repo.get_next_sequence(req.from_account_id)
        to_seq = await self.account_repo.get_next_sequence(req.to_account_id)

        debit_entry = LedgerEntry(
            transaction_id=tx.id,
            account_id=req.from_account_id,
            entry_type=EntryType.DEBIT,
            amount=req.amount,
            currency=req.currency,
            sequence=from_seq,
            description=f"Transfer to {req.to_account_id}: {req.description}",
        )
        credit_entry = LedgerEntry(
            transaction_id=tx.id,
            account_id=req.to_account_id,
            entry_type=EntryType.CREDIT,
            amount=req.amount,
            currency=req.currency,
            sequence=to_seq,
            description=f"Transfer from {req.from_account_id}: {req.description}",
        )

        await self.tx_repo.add_entry(debit_entry)
        await self.tx_repo.add_entry(credit_entry)

        # 4. Verify double-entry invariant before committing
        self._assert_double_entry([
            (EntryType.DEBIT, req.amount),
            (EntryType.CREDIT, req.amount),
        ])

        # 5. Atomic commit — constraint violation on (account_id, sequence)
        #    will rollback and propagate IntegrityError to caller.
        await self.db.commit()
        return await self.tx_repo.get_by_id(tx.id)

    # -----------------------------------------------------------------------
    # Manual journal (N-leg, for accountants / system entries)
    # -----------------------------------------------------------------------

    async def post_journal(
        self, req: ManualJournalRequest, initiated_by: str
    ) -> Transaction:
        # Validate all accounts are active
        accounts = {}
        for leg in req.legs:
            if leg.account_id not in accounts:
                accounts[leg.account_id] = await self._load_active_account(leg.account_id)

        # Re-verify double-entry (belt-and-suspenders; pydantic already checked)
        self._assert_double_entry([(leg.entry_type, leg.amount) for leg in req.legs])

        # Check overdraft for each account being debited
        for leg in req.legs:
            if leg.entry_type == EntryType.DEBIT:
                acc = accounts[leg.account_id]
                balance = await self._get_raw_balance(leg.account_id, acc)
                available = balance + acc.overdraft_limit
                if available < leg.amount:
                    raise InsufficientFundsError(
                        leg.account_id, float(available), float(leg.amount)
                    )

        reference = req.reference or str(uuid.uuid4())
        tx = Transaction(
            reference=reference,
            description=req.description,
            status=TransactionStatus.COMMITTED,
            created_by=initiated_by,
        )
        tx = await self.tx_repo.create(tx)

        for leg in req.legs:
            seq = await self.account_repo.get_next_sequence(leg.account_id)
            entry = LedgerEntry(
                transaction_id=tx.id,
                account_id=leg.account_id,
                entry_type=leg.entry_type,
                amount=leg.amount,
                currency=leg.currency,
                sequence=seq,
                description=leg.description or req.description,
            )
            await self.tx_repo.add_entry(entry)

        await self.db.commit()
        return await self.tx_repo.get_by_id(tx.id)

    # -----------------------------------------------------------------------
    # Reversal
    # -----------------------------------------------------------------------

    async def reverse_transaction(
        self, tx_id: str, reason: str, initiated_by: str
    ) -> Transaction:
        original = await self.tx_repo.get_by_id(tx_id)
        if not original:
            raise ValueError(f"Transaction {tx_id} not found.")
        if original.status == TransactionStatus.REVERSED:
            raise ValueError(f"Transaction {tx_id} is already reversed.")

        # Build a reversal: flip every entry's type
        reversal_ref = f"REVERSAL-{original.reference}"
        reversal_tx = Transaction(
            reference=reversal_ref,
            description=f"Reversal of {original.reference}: {reason}",
            status=TransactionStatus.COMMITTED,
            created_by=initiated_by,
            reverses_id=original.id,
        )
        reversal_tx = await self.tx_repo.create(reversal_tx)

        for entry in original.entries:
            flipped_type = (
                EntryType.CREDIT if entry.entry_type == EntryType.DEBIT else EntryType.DEBIT
            )
            seq = await self.account_repo.get_next_sequence(entry.account_id)
            reversal_entry = LedgerEntry(
                transaction_id=reversal_tx.id,
                account_id=entry.account_id,
                entry_type=flipped_type,
                amount=entry.amount,
                currency=entry.currency,
                sequence=seq,
                description=f"Reversal: {entry.description}",
            )
            await self.tx_repo.add_entry(reversal_entry)

        await self.tx_repo.mark_reversed(original.id)
        await self.db.commit()
        return await self.tx_repo.get_by_id(reversal_tx.id)

    # -----------------------------------------------------------------------
    # Audit trail
    # -----------------------------------------------------------------------

    async def get_audit_trail(self, account_id: str) -> AuditTrailResponse:
        acc = await self.get_account(account_id)
        entries = await self.account_repo.get_entries(account_id)
        total = await self.account_repo.count_entries(account_id)

        # Replay from zero to build running balance
        running = Decimal("0")
        trail: List[AuditTrailEntry] = []

        for e in entries:
            if e.entry_type == EntryType.CREDIT:
                running += e.amount
            else:
                running -= e.amount

            trail.append(AuditTrailEntry(
                sequence=e.sequence,
                event_id=e.id,
                transaction_id=e.transaction_id,
                entry_type=e.entry_type,
                amount=e.amount,
                currency=e.currency,
                running_balance=running,
                description=e.description,
                occurred_at=e.created_at,
            ))

        return AuditTrailResponse(
            account_id=account_id,
            currency=acc.currency,
            current_balance=running,
            entries=trail,
            total_events=total,
        )

    # -----------------------------------------------------------------------
    # Snapshotting (stretch goal)
    # -----------------------------------------------------------------------

    async def create_snapshot(self, account_id: str) -> SnapshotResponse:
        acc = await self.get_account(account_id)
        total_events = await self.account_repo.count_entries(account_id)
        if total_events == 0:
            raise ValueError("Cannot snapshot an account with no events.")

        balance = await self.account_repo.compute_balance(account_id)
        snapshot = AccountSnapshot(
            account_id=account_id,
            balance=balance,
            at_sequence=total_events,
        )
        snapshot = await self.account_repo.create_snapshot(snapshot)
        await self.db.commit()
        return SnapshotResponse.model_validate(snapshot)

    # -----------------------------------------------------------------------
    # Transaction lookup
    # -----------------------------------------------------------------------

    async def get_transaction(self, tx_id: str) -> Transaction:
        tx = await self.tx_repo.get_by_id(tx_id)
        if not tx:
            raise ValueError(f"Transaction {tx_id} not found.")
        return tx

    async def list_account_transactions(
        self, account_id: str, page: int, page_size: int
    ) -> tuple:
        await self.get_account(account_id)  # ensure exists
        return await self.tx_repo.list_for_account(account_id, page, page_size)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    async def _load_active_account(self, account_id: str) -> Account:
        acc = await self.account_repo.get_by_id(account_id)
        if not acc:
            raise AccountNotFoundError(account_id)
        if acc.status == AccountStatus.CLOSED:
            raise AccountClosedError(account_id)
        return acc

    async def _get_raw_balance(self, account_id: str, acc: Account) -> Decimal:
        snapshot = await self.account_repo.get_latest_snapshot(account_id)
        if snapshot:
            return await self.account_repo.compute_balance_from_snapshot(account_id, snapshot)
        return await self.account_repo.compute_balance(account_id)

    @staticmethod
    def _assert_currency(acc: Account, currency: str) -> None:
        if acc.currency != currency.upper():
            raise CurrencyMismatchError(acc.currency, currency.upper())

    @staticmethod
    def _assert_amount(amount: Decimal) -> None:
        if amount <= 0:
            raise NegativeAmountError(float(amount))
        if amount > Decimal(str(settings.max_transaction_amount)):
            raise TransactionAmountError(float(amount), settings.max_transaction_amount)

    @staticmethod
    def _assert_double_entry(legs: list[tuple[EntryType, Decimal]]) -> None:
        net = Decimal("0")
        for entry_type, amount in legs:
            net += amount if entry_type == EntryType.DEBIT else -amount
        if abs(net) > Decimal("0.000001"):
            raise DoubleEntryViolationError(float(net))
