# Event-Sourced Ledger API

A double-entry bookkeeping backend built with FastAPI and async SQLAlchemy. Balances are never stored. Every change is an immutable event, and the current balance is computed from the event history.

## Why I built this

I wanted to understand how payment systems keep money consistent under concurrent writes, so I built the core of one from scratch instead of another CRUD app. The focus is on correctness: derived balances, atomic multi-leg transactions, and a full audit trail.

## How it works

```
Request → API routes → LedgerService → Repositories → DB
```

- `/auth`, `/accounts`, `/transactions` routes contain no business logic
- `LedgerService` holds the domain rules: double-entry validation, overdraft checks, atomic commits
- Repositories handle queries, including balance computation from events
- Runs on PostgreSQL (Docker) or SQLite (local dev)

## Design decisions

**No balance column.** The `accounts` table has no balance. It is always `SUM(credits) - SUM(debits)` over `ledger_events`, so state is derived from history and can't drift out of sync.

**Append-only events.** The application has no code path that updates or deletes rows in `ledger_events`. A `CHECK (amount > 0)` constraint and a unique `(account_id, sequence)` index protect data validity and ordering. Note that append-only is enforced in application code, not by the database itself. Enforcing it at the DB level (Postgres trigger or `REVOKE UPDATE, DELETE`) is a planned improvement.

**Double-entry checked twice.** Pydantic checks `debits == credits` on the request schema, and the service re-checks before commit. If either check fails, nothing is written.

**Atomic transactions.** All legs of a transaction go through one SQLAlchemy session and a single commit. If any leg fails, everything rolls back, so there are no partial transfers.

**Concurrency.** Each account's events carry an increasing sequence number. The unique `(account_id, sequence)` constraint means two concurrent writes to the same account can't both succeed. The losing write raises an `IntegrityError` and is rejected instead of silently overwriting the other (no lost updates).

**Snapshots.** Replaying thousands of events per balance query gets slow, so `account_snapshots` stores checkpoints. Balance = snapshot value + events after it. Snapshots can be deleted and rebuilt at any time because events remain the source of truth.

## Sign convention

Balance is computed as `credits - debits` for every account type. This means an `ASSET` account like Checking goes **up** on a `CREDIT`, which is the opposite of textbook accounting where assets increase on debit. I kept one uniform formula to keep the balance logic simple. To get standard accounting behavior, the sign would need to be flipped per account type in the repository layer.

## Limitations

- Append-only is not yet enforced at the database level
- Uniform sign convention across account types (see above)
- Conflicting concurrent writes to the same account are rejected, so the client has to retry
- Single-currency accounts only: each leg carries a currency, but there is no conversion logic

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

# Fund checking with a two-leg journal entry
# (CREDIT increases balance here, see "Sign convention" above)
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
