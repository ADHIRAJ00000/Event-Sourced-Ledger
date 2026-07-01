"""
SQLAlchemy ORM models.

Design principles:
- `ledger_events` is APPEND-ONLY. No UPDATE or DELETE ever.
- `accounts.balance` column does NOT exist — balance is always derived.
- `account_snapshots` is an optimisation, never the source of truth.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    String, Numeric, DateTime, Enum, ForeignKey,
    UniqueConstraint, Index, CheckConstraint, Integer, Text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AccountType(str, PyEnum):
    ASSET      = "ASSET"       # e.g. checking / savings
    LIABILITY  = "LIABILITY"   # e.g. credit card
    EQUITY     = "EQUITY"
    REVENUE    = "REVENUE"
    EXPENSE    = "EXPENSE"


class AccountStatus(str, PyEnum):
    ACTIVE  = "ACTIVE"
    FROZEN  = "FROZEN"
    CLOSED  = "CLOSED"


class EntryType(str, PyEnum):
    DEBIT  = "DEBIT"
    CREDIT = "CREDIT"


class TransactionStatus(str, PyEnum):
    PENDING   = "PENDING"
    COMMITTED = "COMMITTED"
    REJECTED  = "REJECTED"
    REVERSED  = "REVERSED"


# ---------------------------------------------------------------------------
# Users (API auth layer)
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id:           Mapped[str]      = mapped_column(String(36), primary_key=True, default=_uuid)
    username:     Mapped[str]      = mapped_column(String(64), unique=True, nullable=False)
    email:        Mapped[str]      = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str]   = mapped_column(String(128), nullable=False)
    is_active:    Mapped[bool]     = mapped_column(default=True)
    created_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    accounts: Mapped[list["Account"]] = relationship("Account", back_populates="owner")


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

class Account(Base):
    __tablename__ = "accounts"

    id:               Mapped[str]         = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id:         Mapped[str]         = mapped_column(ForeignKey("users.id"), nullable=False)
    name:             Mapped[str]         = mapped_column(String(128), nullable=False)
    account_type:     Mapped[AccountType] = mapped_column(Enum(AccountType), nullable=False)
    currency:         Mapped[str]         = mapped_column(String(3), nullable=False, default="USD")
    status:           Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus), nullable=False, default=AccountStatus.ACTIVE
    )
    # Overdraft limit in base currency (0 = no overdraft)
    overdraft_limit:  Mapped[Decimal]     = mapped_column(
        Numeric(precision=20, scale=6), nullable=False, default=Decimal("0")
    )
    created_at:       Mapped[datetime]    = mapped_column(DateTime(timezone=True), default=_now)
    updated_at:       Mapped[datetime]    = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    metadata_:        Mapped[Optional[str]] = mapped_column("metadata", Text, nullable=True)

    owner:    Mapped["User"]               = relationship("User", back_populates="accounts")
    entries:  Mapped[list["LedgerEntry"]]  = relationship("LedgerEntry", back_populates="account")
    snapshots: Mapped[list["AccountSnapshot"]] = relationship("AccountSnapshot", back_populates="account")

    __table_args__ = (
        CheckConstraint("overdraft_limit >= 0", name="ck_accounts_overdraft_non_negative"),
    )


# ---------------------------------------------------------------------------
# Transactions (the envelope for a set of double-entry legs)
# ---------------------------------------------------------------------------

class Transaction(Base):
    """
    A transaction groups two or more LedgerEntries that must net to zero.
    It is the atomic unit of change in the ledger.
    """
    __tablename__ = "transactions"

    id:           Mapped[str]               = mapped_column(String(36), primary_key=True, default=_uuid)
    reference:    Mapped[str]               = mapped_column(String(128), unique=True, nullable=False, default=_uuid)
    description:  Mapped[str]               = mapped_column(Text, nullable=False)
    status:       Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus), nullable=False, default=TransactionStatus.COMMITTED
    )
    created_at:   Mapped[datetime]          = mapped_column(DateTime(timezone=True), default=_now, index=True)
    created_by:   Mapped[Optional[str]]     = mapped_column(ForeignKey("users.id"), nullable=True)
    # If this is a reversal, point to the original
    reverses_id:  Mapped[Optional[str]]     = mapped_column(ForeignKey("transactions.id"), nullable=True)
    metadata_:    Mapped[Optional[str]]     = mapped_column("metadata", Text, nullable=True)

    entries:      Mapped[list["LedgerEntry"]] = relationship("LedgerEntry", back_populates="transaction")
    original:     Mapped[Optional["Transaction"]] = relationship(
        "Transaction", remote_side="Transaction.id", foreign_keys=[reverses_id]
    )


# ---------------------------------------------------------------------------
# Ledger Entries (the immutable event stream — NEVER UPDATE, NEVER DELETE)
# ---------------------------------------------------------------------------

class LedgerEntry(Base):
    """
    ONE ROW = ONE double-entry leg.
    IMMUTABLE. No UPDATE, no DELETE ever.
    The entire account balance is the aggregate of these rows.
    """
    __tablename__ = "ledger_events"

    id:             Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str]        = mapped_column(ForeignKey("transactions.id"), nullable=False, index=True)
    account_id:     Mapped[str]        = mapped_column(ForeignKey("accounts.id"), nullable=False, index=True)
    entry_type:     Mapped[EntryType]  = mapped_column(Enum(EntryType), nullable=False)
    amount:         Mapped[Decimal]    = mapped_column(
        Numeric(precision=20, scale=6), nullable=False
    )
    currency:       Mapped[str]        = mapped_column(String(3), nullable=False)
    # Sequence number within the account's own event stream (for ordering/snapshotting)
    sequence:       Mapped[int]        = mapped_column(Integer, nullable=False)
    created_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_now, index=True)
    description:    Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    account:     Mapped["Account"]     = relationship("Account", back_populates="entries")
    transaction: Mapped["Transaction"] = relationship("Transaction", back_populates="entries")

    __table_args__ = (
        # Optimistic concurrency: (account_id, sequence) must be unique
        UniqueConstraint("account_id", "sequence", name="uq_ledger_account_sequence"),
        Index("ix_ledger_account_created", "account_id", "created_at"),
        CheckConstraint("amount > 0", name="ck_ledger_amount_positive"),
    )


# ---------------------------------------------------------------------------
# Snapshots (optimisation — never source of truth)
# ---------------------------------------------------------------------------

class AccountSnapshot(Base):
    """
    Periodic materialized balance checkpoint.
    Balance at sequence N = snapshot.balance + SUM(events after snapshot.sequence)
    """
    __tablename__ = "account_snapshots"

    id:            Mapped[str]     = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id:    Mapped[str]     = mapped_column(ForeignKey("accounts.id"), nullable=False, index=True)
    balance:       Mapped[Decimal] = mapped_column(Numeric(precision=20, scale=6), nullable=False)
    at_sequence:   Mapped[int]     = mapped_column(Integer, nullable=False)
    created_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped["Account"] = relationship("Account", back_populates="snapshots")

    __table_args__ = (
        UniqueConstraint("account_id", "at_sequence", name="uq_snapshot_account_sequence"),
    )
