# Event-Sourced Ledger API

A **production-grade double-entry bookkeeping system** built with FastAPI and SQLAlchemy.

> **Every balance change is an immutable event. Balances are never stored — only derived.**

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    FastAPI App                      │
│  ┌──────────┐  ┌──────────┐  ┌────────────────┐   │
│  │  /auth   │  │/accounts │  │  /transactions │   │
│  └────┬─────┘  └────┬─────┘  └───────┬────────┘   │
│       └─────────────┼────────────────┘             │
│                     ▼                               │
│              LedgerService (domain)                 │
│           ┌────────────────────┐                    │
│           │  Invariant checks: │                    │
│           │  - Double-entry ✓  │                    │
│           │  - Overdraft ✓     │                    │
│           │  - Atomicity ✓     │                    │
│           └─────────┬──────────┘                   │
│                     ▼                               │
│         AccountRepo / TransactionRepo               │
│                     ▼                               │
│  ┌──────────────────────────────────────────────┐  │
│  │             PostgreSQL / SQLite              │  │
│  │                                              │  │
│  │  accounts          ledger_events (IMMUTABLE) │  │
│  │  transactions      account_snapshots         │  │
│  └──────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

## Key Design Decisions

### 1. No `balance` column anywhere
The `accounts` table has **no balance column**. Every balance is computed by `SUM(credits) - SUM(debits)` over the `ledger_events` table. This is event sourcing: state is always derived from history.

### 2. `ledger_events` is append-only
No `UPDATE`, no `DELETE` ever touches this table. The DB constraint `CHECK (amount > 0)` and a unique `(account_id, sequence)` index enforce this at the database level.

### 3. Double-entry enforced at two layers
- **Schema layer (Pydantic)**: `ManualJournalRequest` has a `@model_validator` that verifies `debits == credits` before the request even reaches the service.
- **Service layer**: `_assert_double_entry()` re-verifies before every commit (belt-and-suspenders).

### 4. Atomic transactions
All legs of a transaction (the `LedgerEntry` rows) are written inside a single SQLAlchemy session with a single `await session.commit()`. A failure on any leg rolls back all legs — no partial transfers.

### 5. Optimistic concurrency via `(account_id, sequence)`
Each account's event stream has a monotonically increasing sequence number. The unique constraint `uq_ledger_account_sequence` means concurrent writes to the same account will cause one to fail with an `IntegrityError` rather than silently producing a lost update.

### 6. Snapshot acceleration
`account_snapshots` stores a materialized checkpoint. The balance query becomes:  
`balance = snapshot.balance + SUM(events after snapshot.at_sequence)`  
instead of replaying the full history. Snapshots are never the source of truth — they can always be discarded and recomputed.

---

## Endpoints

### Auth
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/auth/register` | Create user |
| POST | `/api/v1/auth/login` | Get JWT token |
| GET | `/api/v1/auth/me` | Current user info |

### Accounts
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/accounts` | Open account |
| GET | `/api/v1/accounts` | List your accounts |
| GET | `/api/v1/accounts/{id}` | Account details |
| GET | `/api/v1/accounts/{id}/balance` | Current balance (event-derived) |
| GET | `/api/v1/accounts/{id}/balance/history?as_of=` | **Point-in-time balance** |
| GET | `/api/v1/accounts/{id}/audit` | **Full audit trail with running balance** |
| POST | `/api/v1/accounts/{id}/snapshots` | Materialise snapshot |
| DELETE | `/api/v1/accounts/{id}` | Close account |

### Transactions
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/transactions/transfer` | Two-leg transfer |
| POST | `/api/v1/transactions/journal` | N-leg manual journal |
| GET | `/api/v1/transactions/{id}` | Get transaction |
| POST | `/api/v1/transactions/{id}/reverse` | Reverse (chargeback/correction) |
| GET | `/api/v1/transactions/account/{id}` | Paginated account history |

---

## Quick Start

### Local (SQLite — no DB required)

```bash
# Clone and setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Run
uvicorn app.main:app --reload

# Browse API docs
open http://localhost:8000/docs
```

### Docker (PostgreSQL)

```bash
docker compose up --build -d
open http://localhost:8000/docs
```

### Run tests

```bash
pytest tests/ -v
```

---

## Example Flow

```bash
# 1. Register
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "email": "alice@example.com", "password": "securepass"}'

# 2. Login
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -d "username=alice&password=securepass" | jq -r .access_token)

# 3. Open two accounts
CHECKING=$(curl -s -X POST http://localhost:8000/api/v1/accounts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Checking","account_type":"ASSET","currency":"USD","overdraft_limit":"0"}' \
  | jq -r .id)

SAVINGS=$(curl -s -X POST http://localhost:8000/api/v1/accounts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Savings","account_type":"ASSET","currency":"USD","overdraft_limit":"0"}' \
  | jq -r .id)

# 4. Fund checking via journal (debit revenue source, credit checking)
curl -X POST http://localhost:8000/api/v1/transactions/journal \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"description\": \"Initial deposit\",
    \"legs\": [
      {\"account_id\": \"$CHECKING\", \"entry_type\": \"CREDIT\", \"amount\": \"5000\", \"currency\": \"USD\"}
    ]
  }"
# Note: This would fail double-entry check — both legs are required. Use transfer instead.

# 5. Transfer
curl -X POST http://localhost:8000/api/v1/transactions/transfer \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"from_account_id\": \"$CHECKING\",
    \"to_account_id\": \"$SAVINGS\",
    \"amount\": \"1000\",
    \"currency\": \"USD\",
    \"description\": \"Monthly savings\"
  }"

# 6. Check audit trail
curl http://localhost:8000/api/v1/accounts/$CHECKING/audit \
  -H "Authorization: Bearer $TOKEN"

# 7. Point-in-time balance
curl "http://localhost:8000/api/v1/accounts/$CHECKING/balance/history?as_of=2024-01-01T00:00:00Z" \
  -H "Authorization: Bearer $TOKEN"
```

---

## Project Structure

```
ledger/
├── app/
│   ├── api/v1/
│   │   ├── auth.py          # Auth endpoints
│   │   ├── accounts.py      # Account CRUD + balance/audit
│   │   ├── transactions.py  # Transfer, journal, reversal
│   │   └── health.py
│   ├── core/
│   │   ├── config.py        # Settings (pydantic-settings)
│   │   ├── exceptions.py    # Domain exceptions
│   │   └── security.py      # JWT + password hashing
│   ├── db/
│   │   └── session.py       # Async SQLAlchemy engine
│   ├── models/
│   │   └── ledger.py        # ORM models (IMMUTABLE ledger_events)
│   ├── repositories/
│   │   ├── account_repository.py    # Balance computation lives here
│   │   └── transaction_repository.py
│   ├── schemas/
│   │   └── ledger.py        # Pydantic v2 request/response schemas
│   ├── services/
│   │   ├── ledger_service.py  # All domain logic + invariants
│   │   └── auth_service.py
│   └── main.py              # App factory
├── tests/
│   └── test_ledger.py       # 26 integration tests
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## What Makes This Resume-Worthy

| Concept | How it's demonstrated |
|---------|----------------------|
| **Event sourcing** | `ledger_events` is append-only; balances derived by replay |
| **Double-entry accounting** | Enforced at schema AND service layer |
| **Transactional integrity** | Single `session.commit()` for all legs |
| **Optimistic concurrency** | `UNIQUE(account_id, sequence)` prevents lost updates |
| **Point-in-time queries** | `balance/history?as_of=` replays up to a timestamp |
| **Audit trail** | Full event stream with running balance reconstruction |
| **Snapshotting** | `account_snapshots` accelerates replay without losing correctness |
| **Domain exceptions** | Rich exception hierarchy, handled at API boundary |
| **Async Python** | Fully async with `asyncpg` / `aiosqlite` |
| **JWT Auth** | OAuth2 password flow, protected endpoints |
| **Layered architecture** | API → Service → Repository → ORM; no logic in routes |
| **Test coverage** | 26 integration tests covering all invariants |
