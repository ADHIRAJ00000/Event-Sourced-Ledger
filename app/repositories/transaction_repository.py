from typing import Optional, List

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ledger import Transaction, LedgerEntry, TransactionStatus


class TransactionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, transaction: Transaction) -> Transaction:
        self.db.add(transaction)
        await self.db.flush()
        await self.db.refresh(transaction)
        return transaction

    async def add_entry(self, entry: LedgerEntry) -> LedgerEntry:
        self.db.add(entry)
        await self.db.flush()
        return entry

    async def get_by_id(self, tx_id: str) -> Optional[Transaction]:
        result = await self.db.execute(
            select(Transaction)
            .options(selectinload(Transaction.entries))
            .where(Transaction.id == tx_id)
        )
        return result.scalar_one_or_none()

    async def get_by_reference(self, reference: str) -> Optional[Transaction]:
        result = await self.db.execute(
            select(Transaction)
            .options(selectinload(Transaction.entries))
            .where(Transaction.reference == reference)
        )
        return result.scalar_one_or_none()

    async def list_for_account(
        self,
        account_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[List[Transaction], int]:
        """Return transactions that have at least one leg in the given account."""
        # sub-select: transaction_ids that touch this account
        subq = (
            select(LedgerEntry.transaction_id)
            .where(LedgerEntry.account_id == account_id)
            .distinct()
            .subquery()
        )
        base = select(Transaction).where(Transaction.id.in_(select(subq.c.transaction_id)))
        count_result = await self.db.execute(
            select(func.count()).select_from(base.subquery())
        )
        total = count_result.scalar() or 0

        result = await self.db.execute(
            base
            .options(selectinload(Transaction.entries))
            .order_by(Transaction.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result.scalars().all()), total

    async def mark_reversed(self, tx_id: str) -> None:
        from sqlalchemy import update
        await self.db.execute(
            update(Transaction)
            .where(Transaction.id == tx_id)
            .values(status=TransactionStatus.REVERSED)
        )
