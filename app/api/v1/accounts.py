from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.exceptions import AccountNotFoundError, AccountClosedError
from app.db.session import get_db
from app.models.ledger import User
from app.schemas.ledger import (
    AccountCreateRequest, AccountResponse, AccountUpdateRequest,
    BalanceResponse, PointInTimeBalanceResponse, AuditTrailResponse,
    SnapshotResponse,
)
from app.services.ledger_service import LedgerService

router = APIRouter(prefix="/accounts", tags=["Accounts"])


def _handle_domain_errors(e: Exception):
    if isinstance(e, AccountNotFoundError):
        raise HTTPException(status_code=404, detail=str(e))
    if isinstance(e, AccountClosedError):
        raise HTTPException(status_code=409, detail=str(e))
    raise HTTPException(status_code=400, detail=str(e))


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(
    req: AccountCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Open a new ledger account."""
    svc = LedgerService(db)
    account = await svc.create_account(req, owner_id=current_user.id)
    return AccountResponse.model_validate(account)


@router.get("", response_model=List[AccountResponse])
async def list_accounts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all accounts owned by the current user."""
    svc = LedgerService(db)
    accounts = await svc.list_accounts(owner_id=current_user.id)
    return [AccountResponse.model_validate(a) for a in accounts]


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get account details (without balance — see /balance)."""
    svc = LedgerService(db)
    try:
        acc = await svc.get_account(account_id)
    except Exception as e:
        _handle_domain_errors(e)
    return AccountResponse.model_validate(acc)


@router.get("/{account_id}/balance", response_model=BalanceResponse)
async def get_balance(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get the CURRENT balance, always derived from the event stream.
    Uses snapshot acceleration if available.
    """
    svc = LedgerService(db)
    try:
        return await svc.get_balance(account_id)
    except Exception as e:
        _handle_domain_errors(e)


@router.get("/{account_id}/balance/history", response_model=PointInTimeBalanceResponse)
async def get_balance_at(
    account_id: str,
    as_of: datetime = Query(..., description="ISO-8601 datetime, e.g. 2024-01-15T12:00:00Z"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Point-in-time balance query.
    Returns what the balance was at any given moment in the past.
    """
    svc = LedgerService(db)
    try:
        return await svc.get_balance_at(account_id, as_of)
    except Exception as e:
        _handle_domain_errors(e)


@router.get("/{account_id}/audit", response_model=AuditTrailResponse)
async def get_audit_trail(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Full audit trail: every event that ever touched this account,
    with running balance after each event. Reconstructs *how* the
    account reached its current state.
    """
    svc = LedgerService(db)
    try:
        return await svc.get_audit_trail(account_id)
    except Exception as e:
        _handle_domain_errors(e)


@router.post("/{account_id}/snapshots", response_model=SnapshotResponse, status_code=201)
async def create_snapshot(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    [Stretch Goal] Materialise a snapshot of the current balance.
    Future balance queries will use this as their starting point,
    avoiding a full replay of the event log.
    """
    svc = LedgerService(db)
    try:
        return await svc.create_snapshot(account_id)
    except Exception as e:
        _handle_domain_errors(e)


@router.delete("/{account_id}", status_code=204)
async def close_account(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Close an account (marks as CLOSED; events remain immutable)."""
    svc = LedgerService(db)
    try:
        await svc.close_account(account_id)
    except Exception as e:
        _handle_domain_errors(e)
