"""Streamlit page flows; modal/fragment interaction is covered by browser QA."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from cashvibe.store import Store

ROOT = Path(__file__).resolve().parents[1]


def labeled(widgets, label):
    return next(widget for widget in widgets if widget.label == label)


def clean(at):
    assert not list(at.exception), [item.message for item in at.exception]
    assert not list(at.error), [item.value for item in at.error]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REQUIRE_POSTGRES", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("CASHVIBE_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("CASHVIBE_CURRENCY", "INR")
    st.cache_resource.clear()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=25).run()
    persistence = Store()
    try:
        yield at, persistence
    finally:
        st.cache_resource.clear()
        persistence.close()


def signup(at, email="alice@app.test"):
    labeled(at.text_input, "Your name").set_value("Student Alice")
    at.text_input(key="signup_email").set_value(email)
    at.text_input(key="signup_password").set_value("Correct Horse 2026!")
    at.text_input(key="signup_confirm").set_value("Correct Horse 2026!")
    at.checkbox[0].check()
    labeled(at.button, "Create my account →").click().run()
    clean(at)
    code = at.session_state["recovery_code"]
    assert code
    at.checkbox(key="saved_recovery").check().run()
    at.button(key="continue_recovery").click().run()
    clean(at)
    return at.session_state["user"], code


def test_signup_recovery_key_gate_logout_invalid_and_valid_login(app):
    at, persistence = app
    user, recovery = signup(at)
    assert user["email"] == "alice@app.test"
    assert persistence.login(user["email"], "Correct Horse 2026!")["id"] == user["id"]
    at.button(key="sidebar_signout").click().run()
    at.text_input(key="login_email").set_value(user["email"])
    at.text_input(key="login_password").set_value("bad password")
    labeled(at.button, "Sign in →").click().run()
    assert list(at.error)
    at.text_input(key="login_password").set_value("Correct Horse 2026!")
    labeled(at.button, "Sign in →").click().run()
    clean(at)
    assert at.session_state["user"]["id"] == user["id"]


def test_pages_render_seeded_financial_records_and_budget_alerts(app):
    at, persistence = app
    user, _ = signup(at)
    persistence.create_transaction(user["id"], "Allowance", 500000, "Other", "2026-11-01", kind="income")
    persistence.create_transaction(user["id"], "Lunch", 25000, "Food & drinks", "2026-11-15")
    at.date_input(key="viewing_month").set_value(date(2026, 11, 1)).run()
    clean(at)
    for page in ("Transactions", "Insights", "Savings goals", "Settings"):
        at.radio(key="navigation").set_value(page).run()
        clean(at)
    at.radio(key="navigation").set_value("Budgets").run()
    labeled(at.number_input, "Monthly limit (INR)").set_value(200.0)
    labeled(at.button, "Save budget").click().run()
    clean(at)
    assert persistence.list_budgets(user["id"], "2026-11")[0]["amount_cents"] == 20000
    assert any("Budget reached" in item.value for item in at.warning)


def test_spendgenie_page_preview_cancel_and_confirm_are_separate(app):
    at, persistence = app
    user, _ = signup(at)
    at.radio(key="navigation").set_value("SpendGenie").run()
    clean(at)
    at.chat_input[0].set_value("Spent 150 on coffee today").run()
    clean(at)
    assert persistence.list_transactions(user["id"]) == []
    assert at.session_state["agent_proposal"]["data"]["amount_cents"] == 15000
    labeled(at.button, "Cancel").click().run()
    clean(at)
    assert persistence.list_transactions(user["id"]) == []
    at.chat_input[0].set_value("Spent 250 on lunch today").run()
    labeled(at.button, "Confirm & save").click().run()
    clean(at)
    rows = persistence.list_transactions(user["id"])
    assert len(rows) == 1
    assert rows[0]["amount_cents"] == 25000


def test_open_group_view_reads_updates_written_by_another_participant(app):
    at, persistence = app
    alice, _ = signup(at)
    bob = persistence.register("Student Bob", "bob@app.test", "Correct Horse 2026!")["user"]
    group = persistence.create_group(alice["id"], "Campus crew")
    persistence.join_group(bob["id"], group["invite_code"])
    expense = persistence.create_shared_expense(bob["id"], group["id"], "Groceries", 10001, "Food & drinks", "2026-11-15", bob["id"], {alice["id"]: 5000, bob["id"]: 5001})
    at.radio(key="navigation").set_value("Groups").run()
    clean(at)
    assert any("Groceries" in item.label for item in at.expander)
    persistence.update_shared_expense(bob["id"], expense["id"], expense["version"], description="Updated groceries")
    at.run()
    clean(at)
    assert any("Updated groceries" in item.label for item in at.expander)
    assert any(button.label == "Replace invite code" for button in at.button)


def test_password_recovery_revokes_existing_app_session(app):
    at, persistence = app
    user, recovery = signup(at)
    persistence.recover(user["email"], recovery, "A New Passphrase 2026!")
    at.run()
    clean(at)
    assert any(button.label == "Sign in →" for button in at.button)
    assert not any(button.key == "sidebar_signout" for button in at.button)
