from __future__ import annotations

import copy
import json
import urllib.error
from datetime import date

import pytest

from cashvibe.agent import Agent, budget_data, summary_data


@pytest.fixture(autouse=True)
def stable_agent_environment(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("CASHVIBE_CURRENCY", "INR")
    monkeypatch.setattr("cashvibe.agent.today", lambda: date(2026, 11, 15))


def personal(store, user, amount=10000, kind="expense", category="Food & drinks"):
    return store.create_transaction(user["id"], "Recorded expense", amount, category, "2026-11-15", kind=kind)


def tool_call(name, arguments, call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_summary_counts_only_actor_share_and_excludes_settlements_from_costs(store, accounts, shared_group):
    alice, bob, _ = accounts
    personal(store, alice, 20000)
    personal(store, alice, 100000, kind="income", category="Other")
    personal(store, bob, 999999)
    store.create_shared_expense(alice["id"], shared_group["id"], "Shared meal", 30000, "Food & drinks", "2026-11-15", alice["id"], {alice["id"]: 10000, bob["id"]: 20000})
    store.record_settlement(bob["id"], shared_group["id"], alice["id"], 10000)
    data = summary_data(store, alice["id"], "2026-11")
    assert data["income_cents"] == 100000
    assert data["personal_expense_cents"] == 20000
    assert data["shared_expense_cents"] == 10000
    assert data["expense_cents"] == 30000
    assert data["net_cents"] == 70000
    assert data["transaction_count"] == 3


def test_overall_budget_covers_all_costs_and_does_not_double_count_category_limits(store, accounts):
    alice = accounts[0]
    personal(store, alice, 9000)
    personal(store, alice, 3000, category="Transport")
    store.set_budget(alice["id"], "2026-11", "Overall", 20000, threshold=80)
    store.set_budget(alice["id"], "2026-11", "Food & drinks", 10000, threshold=80)
    data = budget_data(store, alice["id"], "2026-11")
    rows = {row["category"]: row for row in data["budgets"]}
    assert rows["Overall"]["spent_cents"] == 12000
    assert rows["Food & drinks"]["status"] == "attention"
    assert data["total_budget_cents"] == 20000
    assert data["combined_remaining_cents"] == 8000


def test_local_expense_is_only_a_proposal_until_explicit_execution(store, accounts):
    alice = accounts[0]
    agent = Agent(store, alice["id"])
    result = agent.respond("Spent 250 on lunch today")
    assert result["provider"] == "Local insights"
    assert result["proposal"]["action"] == "add_transaction"
    assert result["proposal"]["data"]["amount_cents"] == 25000
    assert result["proposal"]["data"]["category"] == "Food & drinks"
    assert store.list_transactions(alice["id"]) == []
    agent.execute_proposal(result["proposal"])
    rows = store.list_transactions(alice["id"])
    assert len(rows) == 1
    assert rows[0]["amount_cents"] == 25000


def test_local_budget_update_is_a_proposal_until_confirmed(store, accounts):
    alice = accounts[0]
    agent = Agent(store, alice["id"])
    result = agent.respond("Set food budget to 3000 this month")
    assert result["proposal"]["action"] == "set_budget"
    assert result["proposal"]["data"]["month"] == "2026-11"
    assert store.list_budgets(alice["id"], "2026-11") == []
    agent.execute_proposal(result["proposal"])
    assert store.list_budgets(alice["id"], "2026-11")[0]["amount_cents"] == 300000


@pytest.mark.parametrize("message", ["Spent -250 on lunch today", "Spent 100 on lunch and 250 on dinner", "Spent 250 tomorrow", "Spent $250 on lunch today"])
def test_ambiguous_negative_future_or_foreign_amounts_do_not_create_records(store, accounts, message):
    agent = Agent(store, accounts[0]["id"])
    response = agent.respond(message)
    assert response["proposal"] is None
    assert store.list_transactions(accounts[0]["id"]) == []


def test_tool_scope_ignores_model_supplied_other_user(store, accounts):
    alice, bob, _ = accounts
    personal(store, alice, 1000)
    personal(store, bob, 999999)
    agent = Agent(store, alice["id"])
    data = agent._run_read_tool("financial_summary", {"month": "2026-11", "user_id": bob["id"]})
    assert data["expense_cents"] == 1000


def test_tampered_proposals_cannot_change_other_users_or_call_arbitrary_actions(store, accounts):
    alice, bob, _ = accounts
    agent = Agent(store, alice["id"])
    proposal = {"action": "add_transaction", "data": {"description": "Lunch", "amount_cents": 15000, "category": "Food & drinks", "occurred_on": "2026-11-15", "user_id": bob["id"]}}
    agent.execute_proposal(proposal)
    assert store.list_transactions(bob["id"]) == []
    assert store.list_transactions(alice["id"])[0]["amount_cents"] == 15000
    with pytest.raises(ValueError):
        agent.execute_proposal({"action": "delete_transaction", "data": {"user_id": bob["id"]}})
    proposal["data"]["amount_cents"] = -1
    with pytest.raises(ValueError):
        agent.execute_proposal(proposal)


def test_ai_tool_cycle_receives_actual_summary_and_no_account_secrets(store, accounts, monkeypatch):
    alice, bob, _ = accounts
    personal(store, alice, 12345)
    personal(store, bob, 999999)
    monkeypatch.setenv("GROQ_API_KEY", "a-test-key-never-used-for-network")
    agent = Agent(store, alice["id"])
    received = []

    def chat(messages):
        received.append(copy.deepcopy(messages))
        if len(received) == 1:
            return {"content": None, "tool_calls": [tool_call("financial_summary", {"month": "2026-11"})]}
        output = json.loads(messages[-1]["content"])
        assert output["expense_cents"] == 12345
        return {"content": "You recorded ₹123.45 in costs this month."}

    monkeypatch.setattr(agent, "_chat_request", chat)
    response = agent.respond("How much did I spend this month?")
    assert response["provider"] == "Groq AI"
    assert "123.45" in response["message"]
    context = json.dumps(received)
    assert alice["email"] not in context
    assert bob["email"] not in context
    assert "Correct Horse" not in context
    assert "a-test-key" not in context


def test_ai_proposal_tool_never_saves_without_confirmation(store, accounts, monkeypatch):
    alice = accounts[0]
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    agent = Agent(store, alice["id"])
    monkeypatch.setattr(agent, "_chat_request", lambda messages: {"tool_calls": [tool_call("propose_transaction", {"description": "Lunch", "amount_cents": 25000, "category": "Food & drinks", "occurred_on": "2026-11-15", "kind": "expense"})]})
    response = agent.respond("Spent 250 on lunch")
    assert response["proposal"]["data"]["amount_cents"] == 25000
    assert store.list_transactions(alice["id"]) == []
    agent.execute_proposal(response["proposal"])
    assert len(store.list_transactions(alice["id"])) == 1


def test_provider_rate_limit_uses_clearly_labeled_local_insights(store, accounts, monkeypatch):
    alice = accounts[0]
    personal(store, alice, 25000)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    agent = Agent(store, alice["id"])

    def unavailable(messages):
        raise urllib.error.HTTPError("https://api.groq.com", 429, "Too many requests", {}, None)

    monkeypatch.setattr(agent, "_chat_request", unavailable)
    response = agent.respond("How much did I spend this month?")
    assert response["provider"] == "Local insights"
    assert "rate limit" in response["message"]
    assert "250.00" in response["message"]
    assert response["proposal"] is None


def test_local_budget_resolves_next_month_and_rejects_malformed_month(store, accounts):
    alice = accounts[0]
    agent = Agent(store, alice["id"])
    next_month = agent.respond("Set food budget to 3000 next month")
    assert next_month["proposal"]["data"]["month"] == "2026-12"
    invalid = agent.respond("Set food budget to 3000 in 2026-13")
    assert invalid["proposal"] is None
    assert store.list_budgets(alice["id"], "2026-11") == []


def test_usd_deployment_accepts_its_currency_and_rejects_foreign_markers(store, accounts, monkeypatch):
    monkeypatch.setenv("CASHVIBE_CURRENCY", "USD")
    agent = Agent(store, accounts[0]["id"])
    own = agent.respond("Spent $12.50 on lunch today")
    assert own["proposal"]["data"]["amount_cents"] == 1250
    foreign = agent.respond("Spent ₹250 on lunch today")
    assert foreign["proposal"] is None
    assert store.list_transactions(accounts[0]["id"]) == []
