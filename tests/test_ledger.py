"""
Full test suite for the Event-Sourced Ledger API.
Tests run against an in-memory SQLite database.
"""

import pytest
import pytest_asyncio
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.db.session import engine, Base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_db():
    """Create tables before each test, drop after."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def auth_headers(client: AsyncClient):
    """Register + login a user, return auth headers."""
    await client.post("/api/v1/auth/register", json={
        "username": "testuser",
        "email": "test@example.com",
        "password": "securepass123",
    })
    resp = await client.post("/api/v1/auth/login", data={
        "username": "testuser",
        "password": "securepass123",
    })
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def checking_account(client: AsyncClient, auth_headers: dict) -> str:
    """Create a checking account, return its ID."""
    resp = await client.post("/api/v1/accounts", json={
        "name": "My Checking",
        "account_type": "ASSET",
        "currency": "USD",
        "overdraft_limit": "0",
    }, headers=auth_headers)
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest_asyncio.fixture
async def savings_account(client: AsyncClient, auth_headers: dict) -> str:
    resp = await client.post("/api/v1/accounts", json={
        "name": "My Savings",
        "account_type": "ASSET",
        "currency": "USD",
        "overdraft_limit": "0",
    }, headers=auth_headers)
    assert resp.status_code == 201
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Helper: fund an account via journal entry
# ---------------------------------------------------------------------------

async def fund_account(
    client: AsyncClient,
    headers: dict,
    account_id: str,
    amount: str,
    source_account_id: str | None = None,
):
    """Post a credit journal to give an account money (simulates deposit)."""
    if source_account_id is None:
        # Use a general equity / external account trick:
        # Create a throwaway revenue account to be the debit side
        resp = await client.post("/api/v1/accounts", json={
            "name": "Revenue Source",
            "account_type": "REVENUE",
            "currency": "USD",
            "overdraft_limit": "9999999",
        }, headers=headers)
        source_id = resp.json()["id"]
    else:
        source_id = source_account_id

    resp = await client.post("/api/v1/transactions/transfer", json={
        "from_account_id": source_id,
        "to_account_id": account_id,
        "amount": amount,
        "currency": "USD",
        "description": "Initial funding",
    }, headers=headers)
    return resp


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

class TestAuth:
    async def test_register_success(self, client: AsyncClient):
        resp = await client.post("/api/v1/auth/register", json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password123",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["username"] == "alice"
        assert "id" in data

    async def test_register_duplicate_username(self, client: AsyncClient):
        payload = {"username": "bob", "email": "bob@example.com", "password": "password123"}
        await client.post("/api/v1/auth/register", json=payload)
        resp = await client.post("/api/v1/auth/register", json={**payload, "email": "bob2@example.com"})
        assert resp.status_code == 409

    async def test_login_success(self, client: AsyncClient):
        await client.post("/api/v1/auth/register", json={
            "username": "carol",
            "email": "carol@example.com",
            "password": "password123",
        })
        resp = await client.post("/api/v1/auth/login", data={
            "username": "carol",
            "password": "password123",
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    async def test_login_wrong_password(self, client: AsyncClient):
        await client.post("/api/v1/auth/register", json={
            "username": "dave",
            "email": "dave@example.com",
            "password": "correcthorse",
        })
        resp = await client.post("/api/v1/auth/login", data={
            "username": "dave",
            "password": "wrongpassword",
        })
        assert resp.status_code == 401

    async def test_me_endpoint(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/api/v1/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["username"] == "testuser"


# ---------------------------------------------------------------------------
# Account tests
# ---------------------------------------------------------------------------

class TestAccounts:
    async def test_create_account(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/api/v1/accounts", json={
            "name": "Checking",
            "account_type": "ASSET",
            "currency": "USD",
            "overdraft_limit": "0",
        }, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Checking"
        assert data["currency"] == "USD"
        assert "balance" not in data or data["balance"] is None

    async def test_list_accounts(self, client, auth_headers, checking_account, savings_account):
        resp = await client.get("/api/v1/accounts", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) >= 2

    async def test_get_balance_zero(self, client, auth_headers, checking_account):
        resp = await client.get(f"/api/v1/accounts/{checking_account}/balance", headers=auth_headers)
        assert resp.status_code == 200
        assert Decimal(resp.json()["balance"]) == Decimal("0")

    async def test_balance_is_derived_not_stored(self, client, auth_headers, checking_account):
        """Balance endpoint must compute from events, not a column."""
        resp = await client.get(f"/api/v1/accounts/{checking_account}", headers=auth_headers)
        account_data = resp.json()
        # The ORM model has no balance column; the response schema has it as optional
        assert "balance" in account_data  # schema field present
        # balance on account detail (without compute) should be null
        assert account_data.get("balance") is None

    async def test_unknown_account_404(self, client, auth_headers):
        resp = await client.get("/api/v1/accounts/nonexistent-id/balance", headers=auth_headers)
        assert resp.status_code == 404

    async def test_unauthenticated_rejected(self, client):
        resp = await client.get("/api/v1/accounts")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Transfer tests (core double-entry logic)
# ---------------------------------------------------------------------------

class TestTransfers:
    async def test_basic_transfer(
        self, client, auth_headers, checking_account, savings_account
    ):
        # Fund checking with 1000
        await fund_account(client, auth_headers, checking_account, "1000.00")

        # Check initial balance
        bal = await client.get(
            f"/api/v1/accounts/{checking_account}/balance", headers=auth_headers
        )
        assert Decimal(bal.json()["balance"]) == Decimal("1000.00")

        # Transfer 250 to savings
        resp = await client.post("/api/v1/transactions/transfer", json={
            "from_account_id": checking_account,
            "to_account_id": savings_account,
            "amount": "250.00",
            "currency": "USD",
            "description": "Monthly savings",
        }, headers=auth_headers)
        assert resp.status_code == 201
        tx = resp.json()
        assert tx["status"] == "COMMITTED"
        assert len(tx["entries"]) == 2

        # Verify balances
        chk = await client.get(f"/api/v1/accounts/{checking_account}/balance", headers=auth_headers)
        sav = await client.get(f"/api/v1/accounts/{savings_account}/balance", headers=auth_headers)
        assert Decimal(chk.json()["balance"]) == Decimal("750.00")
        assert Decimal(sav.json()["balance"]) == Decimal("250.00")

    async def test_double_entry_legs_net_to_zero(
        self, client, auth_headers, checking_account, savings_account
    ):
        """Verifies that DEBIT and CREDIT amounts are equal in every transfer."""
        await fund_account(client, auth_headers, checking_account, "500.00")
        resp = await client.post("/api/v1/transactions/transfer", json={
            "from_account_id": checking_account,
            "to_account_id": savings_account,
            "amount": "200.00",
            "currency": "USD",
            "description": "Test",
        }, headers=auth_headers)
        entries = resp.json()["entries"]
        debit_sum = sum(Decimal(e["amount"]) for e in entries if e["entry_type"] == "DEBIT")
        credit_sum = sum(Decimal(e["amount"]) for e in entries if e["entry_type"] == "CREDIT")
        assert debit_sum == credit_sum, "Double-entry invariant violated!"

    async def test_overdraft_rejected(
        self, client, auth_headers, checking_account, savings_account
    ):
        """Transfer from account with zero balance must be rejected."""
        resp = await client.post("/api/v1/transactions/transfer", json={
            "from_account_id": checking_account,
            "to_account_id": savings_account,
            "amount": "100.00",
            "currency": "USD",
            "description": "Should fail",
        }, headers=auth_headers)
        assert resp.status_code == 422  # Insufficient funds

    async def test_overdraft_within_limit_allowed(
        self, client: AsyncClient, auth_headers, savings_account
    ):
        """Account with overdraft_limit > 0 can go negative within limit."""
        # Create credit account with 500 overdraft limit
        resp = await client.post("/api/v1/accounts", json={
            "name": "Credit Card",
            "account_type": "LIABILITY",
            "currency": "USD",
            "overdraft_limit": "500.00",
        }, headers=auth_headers)
        credit_id = resp.json()["id"]

        # Transfer 300 out (balance starts at 0, limit is 500)
        resp = await client.post("/api/v1/transactions/transfer", json={
            "from_account_id": credit_id,
            "to_account_id": savings_account,
            "amount": "300.00",
            "currency": "USD",
            "description": "Within overdraft",
        }, headers=auth_headers)
        assert resp.status_code == 201

        bal = await client.get(f"/api/v1/accounts/{credit_id}/balance", headers=auth_headers)
        assert Decimal(bal.json()["balance"]) == Decimal("-300.00")

    async def test_same_account_transfer_rejected(
        self, client, auth_headers, checking_account
    ):
        resp = await client.post("/api/v1/transactions/transfer", json={
            "from_account_id": checking_account,
            "to_account_id": checking_account,
            "amount": "100.00",
            "currency": "USD",
            "description": "Self transfer",
        }, headers=auth_headers)
        assert resp.status_code == 422  # Pydantic validation


# ---------------------------------------------------------------------------
# Manual journal tests
# ---------------------------------------------------------------------------

class TestManualJournal:
    async def test_valid_journal(self, client, auth_headers, checking_account, savings_account):
        await fund_account(client, auth_headers, checking_account, "1000.00")
        resp = await client.post("/api/v1/transactions/journal", json={
            "description": "Manual split",
            "legs": [
                {
                    "account_id": checking_account,
                    "entry_type": "DEBIT",
                    "amount": "300.00",
                    "currency": "USD",
                },
                {
                    "account_id": savings_account,
                    "entry_type": "CREDIT",
                    "amount": "300.00",
                    "currency": "USD",
                },
            ],
        }, headers=auth_headers)
        assert resp.status_code == 201

    async def test_unbalanced_journal_rejected(self, client, auth_headers, checking_account, savings_account):
        resp = await client.post("/api/v1/transactions/journal", json={
            "description": "Unbalanced",
            "legs": [
                {"account_id": checking_account, "entry_type": "DEBIT", "amount": "100.00", "currency": "USD"},
                {"account_id": savings_account, "entry_type": "CREDIT", "amount": "99.99", "currency": "USD"},
            ],
        }, headers=auth_headers)
        assert resp.status_code == 422  # Pydantic catches imbalance


# ---------------------------------------------------------------------------
# Audit trail & point-in-time tests
# ---------------------------------------------------------------------------

class TestAuditAndHistory:
    async def test_audit_trail(self, client, auth_headers, checking_account, savings_account):
        await fund_account(client, auth_headers, checking_account, "1000.00")
        await client.post("/api/v1/transactions/transfer", json={
            "from_account_id": checking_account,
            "to_account_id": savings_account,
            "amount": "200.00",
            "currency": "USD",
            "description": "T1",
        }, headers=auth_headers)

        resp = await client.get(
            f"/api/v1/accounts/{checking_account}/audit", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_events"] >= 2
        # Running balance after last entry should match current balance
        last_running = Decimal(data["entries"][-1]["running_balance"])
        bal_resp = await client.get(
            f"/api/v1/accounts/{checking_account}/balance", headers=auth_headers
        )
        assert last_running == Decimal(bal_resp.json()["balance"])

    async def test_running_balance_progression(self, client, auth_headers, checking_account, savings_account):
        """Running balance in audit trail must monotonically track all entries."""
        await fund_account(client, auth_headers, checking_account, "500.00")
        await client.post("/api/v1/transactions/transfer", json={
            "from_account_id": checking_account,
            "to_account_id": savings_account,
            "amount": "100.00",
            "currency": "USD",
            "description": "tx1",
        }, headers=auth_headers)
        await client.post("/api/v1/transactions/transfer", json={
            "from_account_id": checking_account,
            "to_account_id": savings_account,
            "amount": "50.00",
            "currency": "USD",
            "description": "tx2",
        }, headers=auth_headers)

        resp = await client.get(f"/api/v1/accounts/{checking_account}/audit", headers=auth_headers)
        entries = resp.json()["entries"]
        assert Decimal(entries[-1]["running_balance"]) == Decimal("350.00")

    async def test_point_in_time_balance(self, client, auth_headers, checking_account, savings_account):
        """Balance before a transfer should differ from balance after."""
        await fund_account(client, auth_headers, checking_account, "1000.00")
        before = datetime.now(timezone.utc)

        await client.post("/api/v1/transactions/transfer", json={
            "from_account_id": checking_account,
            "to_account_id": savings_account,
            "amount": "400.00",
            "currency": "USD",
            "description": "tx",
        }, headers=auth_headers)

        # Balance as-of before the transfer (should not include the 400 debit)
        resp = await client.get(
            f"/api/v1/accounts/{checking_account}/balance/history",
            params={"as_of": before.isoformat()},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        historical = Decimal(resp.json()["balance"])
        assert historical == Decimal("1000.00")


# ---------------------------------------------------------------------------
# Reversal tests
# ---------------------------------------------------------------------------

class TestReversal:
    async def test_reverse_transfer(self, client, auth_headers, checking_account, savings_account):
        await fund_account(client, auth_headers, checking_account, "1000.00")
        tx_resp = await client.post("/api/v1/transactions/transfer", json={
            "from_account_id": checking_account,
            "to_account_id": savings_account,
            "amount": "300.00",
            "currency": "USD",
            "description": "to be reversed",
        }, headers=auth_headers)
        tx_id = tx_resp.json()["id"]

        # Reverse it
        rev_resp = await client.post(f"/api/v1/transactions/{tx_id}/reverse", json={
            "reason": "Customer requested chargeback",
        }, headers=auth_headers)
        assert rev_resp.status_code == 201

        # Original should be REVERSED
        orig = await client.get(f"/api/v1/transactions/{tx_id}", headers=auth_headers)
        assert orig.json()["status"] == "REVERSED"

        # Balances should be back to pre-transfer state
        chk = await client.get(f"/api/v1/accounts/{checking_account}/balance", headers=auth_headers)
        sav = await client.get(f"/api/v1/accounts/{savings_account}/balance", headers=auth_headers)
        assert Decimal(chk.json()["balance"]) == Decimal("1000.00")
        assert Decimal(sav.json()["balance"]) == Decimal("0.00")

    async def test_double_reverse_rejected(self, client, auth_headers, checking_account, savings_account):
        await fund_account(client, auth_headers, checking_account, "500.00")
        tx = await client.post("/api/v1/transactions/transfer", json={
            "from_account_id": checking_account,
            "to_account_id": savings_account,
            "amount": "100.00",
            "currency": "USD",
            "description": "tx",
        }, headers=auth_headers)
        tx_id = tx.json()["id"]
        await client.post(f"/api/v1/transactions/{tx_id}/reverse", json={"reason": "first"}, headers=auth_headers)
        resp = await client.post(f"/api/v1/transactions/{tx_id}/reverse", json={"reason": "second"}, headers=auth_headers)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Snapshotting tests
# ---------------------------------------------------------------------------

class TestSnapshots:
    async def test_snapshot_creation(self, client, auth_headers, checking_account, savings_account):
        await fund_account(client, auth_headers, checking_account, "1000.00")
        resp = await client.post(
            f"/api/v1/accounts/{checking_account}/snapshots", headers=auth_headers
        )
        assert resp.status_code == 201
        snap = resp.json()
        assert Decimal(snap["balance"]) == Decimal("1000.00")
        assert snap["at_sequence"] == 1

    async def test_balance_consistent_after_snapshot(
        self, client, auth_headers, checking_account, savings_account
    ):
        """Balance from snapshot path must match full-replay path."""
        await fund_account(client, auth_headers, checking_account, "1000.00")

        # Take snapshot
        await client.post(f"/api/v1/accounts/{checking_account}/snapshots", headers=auth_headers)

        # Make another transfer
        await client.post("/api/v1/transactions/transfer", json={
            "from_account_id": checking_account,
            "to_account_id": savings_account,
            "amount": "150.00",
            "currency": "USD",
            "description": "post-snapshot",
        }, headers=auth_headers)

        bal_resp = await client.get(
            f"/api/v1/accounts/{checking_account}/balance", headers=auth_headers
        )
        data = bal_resp.json()
        assert data["from_snapshot"] is True
        assert Decimal(data["balance"]) == Decimal("850.00")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealth:
    async def test_health(self, client: AsyncClient):
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
