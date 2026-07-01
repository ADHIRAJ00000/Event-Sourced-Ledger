from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.exceptions import (
    AccountNotFoundError, AccountClosedError,
    InsufficientFundsError, DoubleEntryViolationError,
    CurrencyMismatchError,
)
from app.db.session import get_db
from app.models.ledger import User
from app.schemas.ledger import (
    TransferRequest, ManualJournalRequest, TransactionResponse,
    ReverseTransactionRequest, PaginatedTransactions,
)
from app.services.ledger_service import LedgerService
import math

router = APIRouter(prefix="/transactions", tags=["Transactions"])


def _map_error(e: Exception) -> HTTPException:
    if isinstance(e, (AccountNotFoundError,)):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, InsufficientFundsError):
        return HTTPException(status_code=422, detail=str(e))
    if isinstance(e, (AccountClosedError, DoubleEntryViolationError, CurrencyMismatchError)):
        return HTTPException(status_code=409, detail=str(e))
    return HTTPException(status_code=400, detail=str(e))


@router.post("/transfer", response_model=TransactionResponse, status_code=201)
async def transfer(
    req: TransferRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Transfer money between two accounts.
    Automatically creates two ledger entries (debit + credit) that net to zero.
    Rejected atomically if the source has insufficient funds.
    """
    svc = LedgerService(db)
    try:
        tx = await svc.transfer(req, initiated_by=current_user.id)
    except Exception as e:
        raise _map_error(e)
    return TransactionResponse.model_validate(tx)


@router.post("/journal", response_model=TransactionResponse, status_code=201)
async def post_journal(
    req: ManualJournalRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Post a multi-leg manual journal entry.
    All legs must sum to zero (debits == credits). Validated at both
    schema layer (Pydantic) and service layer (belt-and-suspenders).
    """
    svc = LedgerService(db)
    try:
        tx = await svc.post_journal(req, initiated_by=current_user.id)
    except Exception as e:
        raise _map_error(e)
    return TransactionResponse.model_validate(tx)


@router.get("/{tx_id}", response_model=TransactionResponse)
async def get_transaction(
    tx_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a transaction and all its ledger legs."""
    svc = LedgerService(db)
    try:
        tx = await svc.get_transaction(tx_id)
    except Exception as e:
        raise _map_error(e)
    return TransactionResponse.model_validate(tx)


@router.post("/{tx_id}/reverse", response_model=TransactionResponse, status_code=201)
async def reverse_transaction(
    tx_id: str,
    req: ReverseTransactionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Reverse a committed transaction.
    Creates a mirror transaction with all entry types flipped.
    The original transaction is marked REVERSED; its events remain immutable.
    """
    svc = LedgerService(db)
    try:
        tx = await svc.reverse_transaction(tx_id, req.reason, initiated_by=current_user.id)
    except Exception as e:
        raise _map_error(e)
    return TransactionResponse.model_validate(tx)


@router.get("/account/{account_id}", response_model=PaginatedTransactions)
async def list_account_transactions(
    account_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Paginated transaction history for an account."""
    svc = LedgerService(db)
    try:
        items, total = await svc.list_account_transactions(account_id, page, page_size)
    except Exception as e:
        raise _map_error(e)
    return PaginatedTransactions(
        items=[TransactionResponse.model_validate(t) for t in items],
        total=total,
        page=page,
        page_size=page_size,
        pages=math.ceil(total / page_size) if total else 0,
    )
