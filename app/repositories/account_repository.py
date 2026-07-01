"""
Account repository — all DB access for accounts and snapshots.
Services call this; never raw SQL in routes.
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ledger import Account, AccountSnapshot, LedgerEntry, EntryType, AccountStatus


class AccountRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, account: Account) -> Account:
        self.db.add(account)
        await self.db.flush()
        await self.db.refresh(account)
        return account

    async def get_by_id(self, account_id: str) -> Optional[Account]:
        result = await self.db.execute(
            select(Account).where(Account.id == account_id)
        )
        return result.scalar_one_or_none()

    async def get_by_owner(self, owner_id: str) -> List[Account]:
        result = await self.db.execute(
            select(Account).where(Account.owner_id == owner_id).order_by(Account.created_at)
        )
        return list(result.scalars().all())

    async def update_status(self, account_id: str, status: AccountStatus) -> None:
        await self.db.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(status=status)
        )

    async def update_fields(self, account_id: str, **fields) -> None:
        await self.db.execute(
            update(Account).where(Account.id == account_id).values(**fields)
        )

    # -----------------------------------------------------------------------
    # Balance computation (DERIVED — never stored)
    # -----------------------------------------------------------------------

    async def get_next_sequence(self, account_id: str) -> int:
        """Returns the next sequence number for an account's event stream."""
        result = await self.db.execute(
            select(func.coalesce(func.max(LedgerEntry.sequence), 0))
            .where(LedgerEntry.account_id == account_id)
        )
        return (result.scalar() or 0) + 1

    async def compute_balance(self, account_id: str) -> Decimal:
        """
        Compute balance from scratch by replaying the entire event stream.
        For asset-type accounts: CREDIT increases balance, DEBIT decreases.
        For liability-type accounts: opposite convention.

        Here we use a simplified universal model:
          balance = SUM(credits) - SUM(debits)
        """
        result = await self.db.execute(
            select(
                func.coalesce(
                    func.sum(
                        # Use CASE-like logic: credits positive, debits negative
                        LedgerEntry.amount
                    ).filter(LedgerEntry.entry_type == EntryType.CREDIT),
                    Decimal("0")
                ) -
                func.coalesce(
                    func.sum(LedgerEntry.amount).filter(LedgerEntry.entry_type == EntryType.DEBIT),
                    Decimal("0")
                )
            ).where(LedgerEntry.account_id == account_id)
        )
        return result.scalar() or Decimal("0")

    async def compute_balance_at(
        self, account_id: str, as_of: datetime
    ) -> tuple[Decimal, int]:
        """Point-in-time balance: replay only events up to `as_of`."""
        result = await self.db.execute(
            select(
                func.coalesce(
                    func.sum(LedgerEntry.amount).filter(LedgerEntry.entry_type == EntryType.CREDIT),
                    Decimal("0")
                ) -
                func.coalesce(
                    func.sum(LedgerEntry.amount).filter(LedgerEntry.entry_type == EntryType.DEBIT),
                    Decimal("0")
                ),
                func.count(LedgerEntry.id)
            ).where(
                LedgerEntry.account_id == account_id,
                LedgerEntry.created_at <= as_of
            )
        )
        row = result.one()
        return (row[0] or Decimal("0")), (row[1] or 0)

    async def compute_balance_from_snapshot(
        self, account_id: str, snapshot: AccountSnapshot
    ) -> Decimal:
        """
        Fast path: snapshot.balance + SUM(events after snapshot.at_sequence).
        This avoids replaying the entire history.
        """
        result = await self.db.execute(
            select(
                func.coalesce(
                    func.sum(LedgerEntry.amount).filter(LedgerEntry.entry_type == EntryType.CREDIT),
                    Decimal("0")
                ) -
                func.coalesce(
                    func.sum(LedgerEntry.amount).filter(LedgerEntry.entry_type == EntryType.DEBIT),
                    Decimal("0")
                )
            ).where(
                LedgerEntry.account_id == account_id,
                LedgerEntry.sequence > snapshot.at_sequence
            )
        )
        delta = result.scalar() or Decimal("0")
        return snapshot.balance + delta

    async def get_latest_snapshot(self, account_id: str) -> Optional[AccountSnapshot]:
        result = await self.db.execute(
            select(AccountSnapshot)
            .where(AccountSnapshot.account_id == account_id)
            .order_by(AccountSnapshot.at_sequence.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create_snapshot(self, snapshot: AccountSnapshot) -> AccountSnapshot:
        self.db.add(snapshot)
        await self.db.flush()
        await self.db.refresh(snapshot)
        return snapshot

    # -----------------------------------------------------------------------
    # Audit trail
    # -----------------------------------------------------------------------

    async def get_entries(
        self,
        account_id: str,
        limit: int = 500,
        offset: int = 0,
    ) -> List[LedgerEntry]:
        result = await self.db.execute(
            select(LedgerEntry)
            .where(LedgerEntry.account_id == account_id)
            .order_by(LedgerEntry.sequence)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_entries(self, account_id: str) -> int:
        result = await self.db.execute(
            select(func.count(LedgerEntry.id))
            .where(LedgerEntry.account_id == account_id)
        )
        return result.scalar() or 0
