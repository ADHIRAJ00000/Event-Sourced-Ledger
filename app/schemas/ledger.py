"""
Pydantic v2 schemas for all API request/response payloads.
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.ledger import AccountType, AccountStatus, EntryType, TransactionStatus


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class UserRegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    email: str = Field(..., pattern=r"^[\w\.-]+@[\w\.-]+\.\w+$")
    password: str = Field(..., min_length=8)


class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    username: str
    password: str


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

class AccountCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    account_type: AccountType
    currency: str = Field("USD", min_length=3, max_length=3)
    overdraft_limit: Decimal = Field(Decimal("0"), ge=0)

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, v: str) -> str:
        return v.upper()


class AccountResponse(BaseModel):
    id: str
    name: str
    account_type: AccountType
    currency: str
    status: AccountStatus
    overdraft_limit: Decimal
    created_at: datetime
    # Balance is NOT stored — it is always computed
    balance: Optional[Decimal] = None

    model_config = {"from_attributes": True}


class AccountUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    status: Optional[AccountStatus] = None
    overdraft_limit: Optional[Decimal] = Field(None, ge=0)


# ---------------------------------------------------------------------------
# Transactions / Transfers
# ---------------------------------------------------------------------------

class TransferRequest(BaseModel):
    """
    Simple two-leg transfer: debit from_account, credit to_account.
    The service layer builds the double-entry legs automatically.
    """
    from_account_id: str
    to_account_id: str
    amount: Decimal = Field(..., gt=0)
    currency: str = Field("USD", min_length=3, max_length=3)
    description: str = Field(..., min_length=1, max_length=512)
    reference: Optional[str] = Field(None, max_length=128)

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def accounts_differ(self) -> "TransferRequest":
        if self.from_account_id == self.to_account_id:
            raise ValueError("from_account_id and to_account_id must differ.")
        return self


class ManualJournalLeg(BaseModel):
    """One leg of a manual journal entry (for accountants)."""
    account_id: str
    entry_type: EntryType
    amount: Decimal = Field(..., gt=0)
    currency: str = Field("USD", min_length=3, max_length=3)
    description: Optional[str] = None


class ManualJournalRequest(BaseModel):
    """
    Post an arbitrary double-entry journal with N legs.
    The service will verify that SUM(debits) == SUM(credits).
    """
    description: str = Field(..., min_length=1)
    reference: Optional[str] = Field(None, max_length=128)
    legs: List[ManualJournalLeg] = Field(..., min_length=2)

    @model_validator(mode="after")
    def validate_double_entry(self) -> "ManualJournalRequest":
        net = Decimal("0")
        for leg in self.legs:
            if leg.entry_type == EntryType.DEBIT:
                net += leg.amount
            else:
                net -= leg.amount
        if abs(net) > Decimal("0.000001"):
            raise ValueError(
                f"Double-entry invariant: debits must equal credits. Net: {net}"
            )
        return self


class LedgerEntryResponse(BaseModel):
    id: int
    transaction_id: str
    account_id: str
    entry_type: EntryType
    amount: Decimal
    currency: str
    sequence: int
    created_at: datetime
    description: Optional[str]

    model_config = {"from_attributes": True}


class TransactionResponse(BaseModel):
    id: str
    reference: str
    description: str
    status: TransactionStatus
    created_at: datetime
    entries: List[LedgerEntryResponse] = []

    model_config = {"from_attributes": True}


class ReverseTransactionRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=512)


# ---------------------------------------------------------------------------
# Audit / Balance queries
# ---------------------------------------------------------------------------

class BalanceResponse(BaseModel):
    account_id: str
    currency: str
    balance: Decimal
    computed_at: datetime
    from_snapshot: bool = False
    snapshot_sequence: Optional[int] = None


class PointInTimeBalanceResponse(BaseModel):
    account_id: str
    currency: str
    balance: Decimal
    as_of: datetime
    entry_count: int


class AuditTrailEntry(BaseModel):
    sequence: int
    event_id: int
    transaction_id: str
    entry_type: EntryType
    amount: Decimal
    currency: str
    running_balance: Decimal
    description: Optional[str]
    occurred_at: datetime


class AuditTrailResponse(BaseModel):
    account_id: str
    currency: str
    current_balance: Decimal
    entries: List[AuditTrailEntry]
    total_events: int


class SnapshotResponse(BaseModel):
    id: str
    account_id: str
    balance: Decimal
    at_sequence: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class PaginatedTransactions(BaseModel):
    items: List[TransactionResponse]
    total: int
    page: int
    page_size: int
    pages: int
