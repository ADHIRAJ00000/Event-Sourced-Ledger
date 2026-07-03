# Event-Sourced Ledger API

A double-entry bookkeeping backend built with FastAPI and async SQLAlchemy. Balances are never stored directly — every balance change is recorded as an immutable event, and the current balance is computed from the event history.

## How it works

```
Request → API routes → LedgerService → Repositories → DB
```

- `/auth`, `/accounts`, `/transactions` routes contain no business logic
- `LedgerService` holds all domain rules: double-entry validation, overdraft checks, atomic commits
- Repositories handle queries, including balance computation from events
- Works with PostgreSQL (Docker) or SQLite (local dev)

## Design notes

**No balance column.** The `accounts` table doesn't store a balance. It's always computed as `SUM(credits) - SUM(debits)` over `ledger_events`. State is derived from history, never stored as mutable data.

**Append-only events.** Nothing ever updates or deletes rows in `ledger_events`. A `CHECK (amount > 0)` constraint and a unique `(account_id, sequence)` index enforce this at the DB level, not just in application code.

**Double-entry checked twice.** Pydantic validates `debits == credits` on the request schema, and the service re-checks before commit. If either fails, nothing is written.

**Atomic transactions.** All legs of a transfer go into one SQLAlchemy session with a single commit. If any leg fails, everything rolls back — no partial transfers.

**Concurrency handling.** Each account's events get a monotonically increasing sequence number. The unique constraint on `(account_id, sequence)` means two concurrent writes to the same account can't both succeed silently — one fails with an `IntegrityError` instead of causing a lost update.

**Snapshots.** Replaying thousands of events per balance query gets slow, so `account_snapshots` stores checkpoints. Balance = snapshot value + events after the snapshot. Snapshots can be deleted and recomputed anytime since events remain the source of truth.

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
| GET | `/api/v1/accounts/{id}/balance` | Current balance |
| GET | `/api/v1/accounts/{id}/balance/history?as_of=` | Balance at a past timestamp |
| GET | `/api/v1/accounts/{id}/audit` | Full event history with running balance |
| POST | `/api/v1/accounts/{id}/snapshots` | Create snapshot |
| DELETE | `/api/v1/accounts/{id}` | Close account |

### Transactions
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/transactions/transfer` | Two-leg transfer |
| POST | `/api/v1/transactions/journal` | N-leg manual journal entry |
| GET | `/api/v1/transactions/{id}` | Get transaction |
| POST | `/api/v1/transactions/{id}/reverse` | Reverse a transaction |
| GET | `/api/v1/transactions/account/{id}` | Paginated account history |

## Setup

Local with SQLite (no DB needed):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

API docs at http://localhost:8000/docs

With Docker (PostgreSQL):

```bash
docker compose up --build -d
```

Tests:

```bash
pytest tests/ -v
```

## Example usage

```bash
# Register and login
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "email": "alice@example.com", "password": "securepass"}'

TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -d "username=alice&password=securepass" | jq -r .access_token)

# Open two accounts
CHECKING=$(curl -s -X POST http://localhost:8000/api/v1/accounts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Checking","account_type":"ASSET","currency":"USD","overdraft_limit":"0"}' \
  | jq -r .id)

EQUITY=$(curl -s -X POST http://localhost:8000/api/v1/accounts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Opening Balance","account_type":"EQUITY","currency":"USD","overdraft_limit":"0"}' \
  | jq -r .id)

# Fund checking with a two-leg journal entry (double-entry requires both sides)
curl -X POST http://localhost:8000/api/v1/transactions/journal \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"description\": \"Initial deposit\",
    \"legs\": [
      {\"account_id\": \"$EQUITY\",   \"entry_type\": \"DEBIT\",  \"amount\": \"5000\", \"currency\": \"USD\"},
      {\"account_id\": \"$CHECKING\", \"entry_type\": \"CREDIT\", \"amount\": \"5000\", \"currency\": \"USD\"}
    ]
  }"

# Check the audit trail
curl http://localhost:8000/api/v1/accounts/$CHECKING/audit \
  -H "Authorization: Bearer $TOKEN"

# Balance at a point in time
curl "http://localhost:8000/api/v1/accounts/$CHECKING/balance/history?as_of=2024-01-01T00:00:00Z" \
  -H "Authorization: Bearer $TOKEN"
```

## Project structure

```
ledger/
├── app/
│   ├── api/v1/          # Route handlers (auth, accounts, transactions)
│   ├── core/            # Config, security (JWT), domain exceptions
│   ├── db/              # Async SQLAlchemy engine/session
│   ├── models/          # ORM models
│   ├── repositories/    # DB queries, balance computation
│   ├── schemas/         # Pydantic request/response models
│   ├── services/        # Domain logic and invariants
│   └── main.py
├── tests/               # 26 integration tests
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
