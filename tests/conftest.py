from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def store(tmp_path):
    from cashvibe.store import Store

    url = os.getenv("CASHVIBE_TEST_POSTGRES_URL")
    if url:
        # Integration tests deliberately accept only our disposable loopback DB.
        # This suite must never erase a deployed user's CashVibe schema.
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url
        parsed = make_url(url)
        if (parsed.host, parsed.port, parsed.database) != ("127.0.0.1", 55432, "cashvibe_qa"):
            raise RuntimeError("PostgreSQL tests require the isolated localhost:55432/cashvibe_qa container.")
        cleanup = create_engine(url)
        with cleanup.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS cashvibe CASCADE"))
        cleanup.dispose()
    instance = Store(url or f"sqlite:///{tmp_path / 'ledger.db'}")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def accounts(store):
    users = []
    for label in ("Alice", "Bob", "Charlie"):
        registration = store.register(label, f"{label.lower()}@example.test", "Correct Horse 2026!")
        users.append(registration["user"])
    return users


@pytest.fixture
def shared_group(store, accounts):
    alice, bob, charlie = accounts
    group = store.create_group(alice["id"], "Campus flat")
    store.join_group(bob["id"], group["invite_code"])
    return group
