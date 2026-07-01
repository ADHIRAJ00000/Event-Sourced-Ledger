"""
Point the test suite at an isolated in-memory SQLite database.

This must run before `app.db.session` is imported anywhere, so it lives in
conftest.py (pytest loads it before collecting test modules). Without this,
tests fall back to the .env `LEDGER_DATABASE_URL` — the real dev database —
and the per-test create/drop-all fixture in test_ledger.py would wipe it.
"""

import os

os.environ["LEDGER_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
