"""
Application factory.
"""

from pathlib import Path

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.exceptions import (
    LedgerException, InsufficientFundsError, AccountNotFoundError,
    DoubleEntryViolationError,
)
from app.db.session import create_all_tables
from app.api.v1 import api_router

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # On startup: create tables (in prod, use Alembic migrations instead)
    await create_all_tables()
    yield
    # On shutdown: nothing needed for SQLite; for Postgres, dispose pool


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="""
## Event-Sourced Ledger API

A production-grade double-entry bookkeeping system where **every balance change
is an immutable event** — never an UPDATE on a balance column.

### Key principles
- **Event sourcing**: balances are always *derived* by replaying the event stream
- **Double-entry**: every transaction has debits and credits that net to zero
- **Point-in-time queries**: know the balance at any past moment
- **Atomic transactions**: no partial transfers — all legs commit or none do
- **Audit trail**: reconstruct exactly how any account reached its current state
- **Snapshotting**: optimise replay performance without sacrificing correctness
        """,
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # ---------------------------------------------------------------------------
    # CORS
    # ---------------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Tighten in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---------------------------------------------------------------------------
    # Domain exception handlers
    # ---------------------------------------------------------------------------

    @app.exception_handler(InsufficientFundsError)
    async def insufficient_funds_handler(request: Request, exc: InsufficientFundsError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": str(exc), "type": "insufficient_funds"},
        )

    @app.exception_handler(AccountNotFoundError)
    async def account_not_found_handler(request: Request, exc: AccountNotFoundError):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(exc), "type": "account_not_found"},
        )

    @app.exception_handler(DoubleEntryViolationError)
    async def double_entry_handler(request: Request, exc: DoubleEntryViolationError):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "type": "double_entry_violation"},
        )

    @app.exception_handler(LedgerException)
    async def ledger_exception_handler(request: Request, exc: LedgerException):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc), "type": "ledger_error"},
        )

    # ---------------------------------------------------------------------------
    # Routers
    # ---------------------------------------------------------------------------
    app.include_router(api_router)

    # ---------------------------------------------------------------------------
    # Web UI (static single-page app served from app/static)
    # ---------------------------------------------------------------------------
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_ui():
        return FileResponse(static_dir / "index.html")

    return app


app = create_app()
