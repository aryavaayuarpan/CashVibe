from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest


AUTH_ERRORS = (ValueError, PermissionError)


def expense(store, user, **overrides):
    values = dict(
        description="Lunch",
        amount_cents=12550,
        category="Food & drinks",
        occurred_on="2026-11-15",
        kind="expense",
        payment_method="UPI",
        notes="Canteen",
    )
    values.update(overrides)
    return store.create_transaction(user["id"], **values)


def shared_expense(store, accounts, group, **overrides):
    alice, bob, _ = accounts
    values = dict(
        group_id=group["id"],
        description="Groceries",
        amount_cents=10001,
        category="Food & drinks",
        occurred_on="2026-11-15",
        payer_id=alice["id"],
        shares={alice["id"]: 5001, bob["id"]: 5000},
        notes="Shared kitchen",
    )
    values.update(overrides)
    return store.create_shared_expense(alice["id"], **values)


def balances(store, user, group):
    detail = store.group_details(user["id"], group["id"])
    return {row["user_id"]: row["balance_cents"] for row in detail["balances"]}


def test_registration_login_normalizes_email_and_never_returns_credentials(store):
    registration = store.register("Alice", " ALICE@Example.Test ", "Correct Horse 2026!")
    user = store.login("alice@example.test", "Correct Horse 2026!")
    assert user["id"] == registration["user"]["id"]
    assert user["email"] == "alice@example.test"
    assert registration["recovery_code"]
    assert all("password" not in key and "recovery" not in key for key in user)
    with pytest.raises(AUTH_ERRORS):
        store.register("Another Alice", "alice@example.test", "Different Horse 2026!")
    with pytest.raises(AUTH_ERRORS):
        store.login("alice@example.test", "wrong password")


def test_recovery_rotates_code_password_and_existing_session_version(store):
    registered = store.register("Alice", "alice@example.test", "Correct Horse 2026!")
    user = registered["user"]
    old_version = store.get_user(user["id"])["auth_version"]
    with pytest.raises(AUTH_ERRORS):
        store.recover(user["email"], "not-the-recovery-code", "New Secret Password 2026!")
    new_code = store.recover(user["email"], registered["recovery_code"], "New Secret Password 2026!")
    assert new_code and new_code != registered["recovery_code"]
    assert store.get_user(user["id"])["auth_version"] > old_version
    with pytest.raises(AUTH_ERRORS):
        store.login(user["email"], "Correct Horse 2026!")
    assert store.login(user["email"], "New Secret Password 2026!")["id"] == user["id"]
    with pytest.raises(AUTH_ERRORS):
        store.recover(user["email"], registered["recovery_code"], "Another Password 2026!")


def test_repeated_bad_login_is_limited(store):
    registered = store.register("Alice", "alice@example.test", "Correct Horse 2026!")
    for _ in range(8):
        with pytest.raises(AUTH_ERRORS):
            store.login("alice@example.test", "wrong password")
    with pytest.raises(AUTH_ERRORS):
        store.login("alice@example.test", "Correct Horse 2026!")
    assert store.get_user(registered["user"]["id"])["name"] == "Alice"


@pytest.mark.parametrize("amount", [0, -1, 1.5, True])
def test_personal_expense_rejects_non_positive_or_non_integer_minor_units(store, accounts, amount):
    with pytest.raises(AUTH_ERRORS):
        expense(store, accounts[0], amount_cents=amount)
    assert store.list_transactions(accounts[0]["id"]) == []


def test_personal_crud_is_authorized_and_stale_edits_are_rejected(store, accounts):
    alice, bob, _ = accounts
    row = expense(store, alice)
    assert store.list_transactions(bob["id"]) == []
    changes = dict(description="Dinner", amount_cents=25000, category="Food & drinks", occurred_on="2026-11-16", kind="expense", payment_method="Cash", notes="Updated")
    with pytest.raises(AUTH_ERRORS):
        store.update_transaction(bob["id"], row["id"], row["version"], **changes)
    with pytest.raises(AUTH_ERRORS):
        store.delete_transaction(bob["id"], row["id"], row["version"])
    store.update_transaction(alice["id"], row["id"], row["version"], **changes)
    updated = store.list_transactions(alice["id"])[0]
    assert updated["description"] == "Dinner"
    assert updated["amount_cents"] == 25000
    assert updated["version"] > row["version"]
    with pytest.raises(AUTH_ERRORS):
        store.update_transaction(alice["id"], row["id"], row["version"], **changes)
    with pytest.raises(AUTH_ERRORS):
        store.delete_transaction(alice["id"], row["id"], row["version"])
    store.delete_transaction(alice["id"], row["id"], updated["version"])
    assert store.list_transactions(alice["id"]) == []


def test_month_filter_and_income_kind_are_preserved(store, accounts):
    alice = accounts[0]
    expense(store, alice, description="October", occurred_on="2026-10-31")
    expense(store, alice, description="Allowance", kind="income", category="Other", amount_cents=500000)
    rows = store.list_transactions(alice["id"], month="2026-11")
    assert len(rows) == 1
    assert rows[0]["kind"] == "income"
    assert rows[0]["amount_cents"] == 500000


def test_groups_require_membership_and_invite_rotation_is_owner_only(store, accounts, shared_group):
    alice, bob, charlie = accounts
    assert len(store.list_groups(alice["id"])) == 1
    assert store.list_groups(charlie["id"]) == []
    with pytest.raises(AUTH_ERRORS):
        store.group_details(charlie["id"], shared_group["id"])
    with pytest.raises(AUTH_ERRORS):
        store.rotate_invite(bob["id"], shared_group["id"])
    store.rotate_invite(alice["id"], shared_group["id"])
    with pytest.raises(AUTH_ERRORS):
        store.join_group(charlie["id"], shared_group["invite_code"])
    assert len(store.group_details(alice["id"], shared_group["id"])["members"]) == 2


def test_shared_expense_preserves_every_cent_and_exposes_only_personal_share(store, accounts, shared_group):
    alice, bob, _ = accounts
    shared_expense(store, accounts, shared_group, amount_cents=101, shares={alice["id"]: 51, bob["id"]: 50})
    detail = store.group_details(bob["id"], shared_group["id"])
    assert sum(v["amount_cents"] for v in detail["expenses"][0]["shares"]) == 101
    assert balances(store, alice, shared_group) == {alice["id"]: 50, bob["id"]: -50}
    assert store.list_shared_shares(alice["id"], month="2026-11")[0]["amount_cents"] == 51
    assert store.list_shared_shares(bob["id"], month="2026-11")[0]["amount_cents"] == 50
    assert store.list_shared_shares(alice["id"], month="2026-10") == []
    assert store.list_transactions(alice["id"]) == []


def test_invalid_shares_or_payers_are_atomic_and_nonmembers_cannot_write(store, accounts, shared_group):
    alice, bob, charlie = accounts
    for changes in (
        {"shares": {alice["id"]: 5000, bob["id"]: 5000}},
        {"shares": {alice["id"]: 5001, charlie["id"]: 5000}},
        {"payer_id": charlie["id"]},
        {"shares": {alice["id"]: -1, bob["id"]: 10002}},
    ):
        with pytest.raises(AUTH_ERRORS):
            shared_expense(store, accounts, shared_group, **changes)
    with pytest.raises(AUTH_ERRORS):
        store.create_shared_expense(charlie["id"], shared_group["id"], "Intrusion", 100, "Other", "2026-11-15", alice["id"], {alice["id"]: 100})
    detail = store.group_details(alice["id"], shared_group["id"])
    assert detail["expenses"] == []
    assert all(value == 0 for value in balances(store, alice, shared_group).values())


def test_shared_updates_visible_to_other_member_and_stale_versions_fail(store, accounts, shared_group):
    alice, bob, charlie = accounts
    row = shared_expense(store, accounts, shared_group)
    changes = dict(description="Weekly groceries", amount_cents=20000, category="Food & drinks", occurred_on="2026-11-16", payer_id=alice["id"], shares={alice["id"]: 10000, bob["id"]: 10000}, notes="Corrected")
    with pytest.raises(AUTH_ERRORS):
        store.update_shared_expense(charlie["id"], row["id"], row["version"], **changes)
    store.update_shared_expense(alice["id"], row["id"], row["version"], **changes)
    detail = store.group_details(bob["id"], shared_group["id"])
    updated = detail["expenses"][0]
    assert updated["description"] == "Weekly groceries"
    assert updated["amount_cents"] == 20000
    assert updated["version"] > row["version"]
    assert balances(store, bob, shared_group)[bob["id"]] == -10000
    with pytest.raises(AUTH_ERRORS):
        store.update_shared_expense(alice["id"], row["id"], row["version"], **changes)
    with pytest.raises(AUTH_ERRORS):
        store.delete_shared_expense(alice["id"], row["id"], row["version"])
    store.delete_shared_expense(alice["id"], row["id"], updated["version"])
    assert store.group_details(bob["id"], shared_group["id"])["expenses"] == []


def test_only_creator_or_owner_can_change_shared_expense(store, accounts, shared_group):
    alice, bob, _ = accounts
    row = shared_expense(store, accounts, shared_group)
    with pytest.raises(AUTH_ERRORS):
        store.delete_shared_expense(bob["id"], row["id"], row["version"])
    assert len(store.group_details(bob["id"], shared_group["id"])["expenses"]) == 1


def test_settlement_reconciles_balances_without_changing_spending(store, accounts, shared_group):
    alice, bob, _ = accounts
    shared_expense(store, accounts, shared_group)
    before_share = store.list_shared_shares(bob["id"])[0]["amount_cents"]
    store.record_settlement(bob["id"], shared_group["id"], alice["id"], 2000)
    assert balances(store, alice, shared_group) == {alice["id"]: 3000, bob["id"]: -3000}
    store.record_settlement(bob["id"], shared_group["id"], alice["id"], 3000)
    assert all(value == 0 for value in balances(store, alice, shared_group).values())
    assert store.list_shared_shares(bob["id"])[0]["amount_cents"] == before_share
    assert len(store.group_details(alice["id"], shared_group["id"])["settlements"]) == 2


def test_settlements_reject_overpayment_self_payment_and_nonmembers(store, accounts, shared_group):
    alice, bob, charlie = accounts
    shared_expense(store, accounts, shared_group)
    for payer, recipient, amount in (
        (bob, alice, 5001),
        (alice, bob, 1),
        (bob, bob, 1),
        (charlie, alice, 1),
        (bob, alice, -1),
        (bob, alice, 0),
    ):
        with pytest.raises(AUTH_ERRORS):
            store.record_settlement(payer["id"], shared_group["id"], recipient["id"], amount)
    assert store.group_details(alice["id"], shared_group["id"])["settlements"] == []
    assert balances(store, alice, shared_group)[bob["id"]] == -5000


def test_budget_upserts_do_not_cross_users_or_months(store, accounts):
    alice, bob, _ = accounts
    store.set_budget(alice["id"], "2026-11", "Food & drinks", 300000, threshold=80)
    store.set_budget(alice["id"], "2026-11", "Food & drinks", 400000, threshold=75)
    store.set_budget(alice["id"], "2026-12", "Food & drinks", 200000, threshold=90)
    budget = store.list_budgets(alice["id"], "2026-11")
    assert len(budget) == 1
    assert budget[0]["amount_cents"] == 400000
    assert budget[0]["threshold"] == 75
    assert store.list_budgets(bob["id"], "2026-11") == []
    assert store.list_budgets(alice["id"], "2026-12")[0]["amount_cents"] == 200000


def test_budget_rejects_invalid_amount_month_category_and_threshold(store, accounts):
    alice = accounts[0]
    for month, category, amount, threshold in (
        ("2026-13", "Food & drinks", 10000, 80),
        ("2026-11", "", 10000, 80),
        ("2026-11", "Food & drinks", -1, 80),
        ("2026-11", "Food & drinks", 10000, 101),
        ("2026-11", "Food & drinks", 10000, 0),
    ):
        with pytest.raises(AUTH_ERRORS):
            store.set_budget(alice["id"], month, category, amount, threshold=threshold)
    assert store.list_budgets(alice["id"], "2026-11") == []


def test_goals_are_private_and_contributions_cannot_exceed_target(store, accounts):
    alice, bob, _ = accounts
    goal = store.create_goal(alice["id"], "Laptop", 100000, "2027-03-01")
    assert store.list_goals(bob["id"]) == []
    with pytest.raises(AUTH_ERRORS):
        store.contribute_goal(bob["id"], goal["id"], 1000)
    store.contribute_goal(alice["id"], goal["id"], 40000)
    for invalid in (-1, 0, 60001):
        with pytest.raises(AUTH_ERRORS):
            store.contribute_goal(alice["id"], goal["id"], invalid)
    store.contribute_goal(alice["id"], goal["id"], 60000)
    saved = store.list_goals(alice["id"])[0]
    assert saved["saved_cents"] == 100000


def test_concurrent_edits_cannot_overwrite_newer_version(store, accounts):
    alice = accounts[0]
    row = expense(store, alice)

    def edit(amount):
        try:
            store.update_transaction(
                alice["id"], row["id"], row["version"],
                description="Same receipt", amount_cents=amount,
                category="Food & drinks", occurred_on="2026-11-15",
                kind="expense", payment_method="UPI", notes="",
            )
            return True
        except AUTH_ERRORS:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(edit, (20000, 30000)))
    assert sorted(outcomes) == [False, True]
    assert store.list_transactions(alice["id"])[0]["amount_cents"] in (20000, 30000)


def test_concurrent_goal_contributions_preserve_both_updates(store, accounts):
    alice = accounts[0]
    goal = store.create_goal(alice["id"], "Laptop", 100000, "2027-03-01")

    def contribute(amount):
        return store.contribute_goal(alice["id"], goal["id"], amount)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(contribute, (20000, 30000)))
    assert store.list_goals(alice["id"])[0]["saved_cents"] == 50000


def test_simultaneous_settlements_cannot_overpay_same_debt(store, accounts, shared_group):
    alice, bob, _ = accounts
    shared_expense(store, accounts, shared_group)

    def settle(_):
        try:
            store.record_settlement(bob["id"], shared_group["id"], alice["id"], 5000)
            return True
        except AUTH_ERRORS:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(settle, range(2)))
    assert sorted(outcomes) == [False, True]
    assert all(value == 0 for value in balances(store, alice, shared_group).values())


def test_settlement_correction_requires_sender_or_group_owner_and_restores_debt(store, accounts, shared_group):
    alice, bob, charlie = accounts
    shared_expense(store, accounts, shared_group)
    settlement = store.record_settlement(bob["id"], shared_group["id"], alice["id"], 5000)
    with pytest.raises(AUTH_ERRORS):
        store.delete_settlement(charlie["id"], settlement["id"])
    assert all(value == 0 for value in balances(store, alice, shared_group).values())
    store.delete_settlement(bob["id"], settlement["id"])
    assert balances(store, alice, shared_group)[bob["id"]] == -5000
    assert store.group_details(alice["id"], shared_group["id"])["settlements"] == []


def test_settlement_recipient_cannot_delete_another_members_transfer(store, accounts, shared_group):
    alice, bob, _ = accounts
    store.create_shared_expense(alice["id"], shared_group["id"], "Bus ticket", 10000, "Transport", "2026-11-15", bob["id"], {alice["id"]: 10000})
    settlement = store.record_settlement(alice["id"], shared_group["id"], bob["id"], 10000)
    with pytest.raises(AUTH_ERRORS):
        store.delete_settlement(bob["id"], settlement["id"])
    assert len(store.group_details(bob["id"], shared_group["id"])["settlements"]) == 1


def test_group_view_uses_consistent_snapshot_during_another_devices_insert(store, accounts, shared_group):
    from sqlalchemy import event

    alice, bob, _ = accounts
    shared_expense(store, accounts, shared_group, description="Original meal")
    injected = []

    def add_between_queries(conn, cursor, statement, parameters, context, executemany):
        if injected or not statement.lstrip().upper().startswith("SELECT"):
            return
        if not ("FROM shared_expenses" in statement or "FROM cashvibe.shared_expenses" in statement) or "JOIN" in statement:
            return
        injected.append(True)
        shared_expense(store, accounts, shared_group, description="Added on another device")

    event.listen(store.engine, "after_cursor_execute", add_between_queries)
    try:
        first_view = store.group_details(bob["id"], shared_group["id"])
    finally:
        event.remove(store.engine, "after_cursor_execute", add_between_queries)
    assert injected
    assert len(first_view["expenses"]) == 1
    assert len(first_view["expenses"][0]["shares"]) == 2
    second_view = store.group_details(bob["id"], shared_group["id"])
    assert len(second_view["expenses"]) == 2
