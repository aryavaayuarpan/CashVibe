from __future__ import annotations

import pytest

from cashvibe.ui import debt_plan, equal_split, percentage_split, to_cents


def test_equal_split_allocates_every_cent_for_indivisible_total():
    shares = equal_split(100, ["alice", "bob", "charlie"])
    assert sum(shares.values()) == 100
    assert max(shares.values()) - min(shares.values()) == 1
    assert all(isinstance(value, int) for value in shares.values())


def test_percentage_split_rounds_fractional_cents_without_loss():
    shares = percentage_split(101, {"alice": 33.33, "bob": 33.33, "charlie": 33.34})
    assert sum(shares.values()) == 101
    assert shares["charlie"] >= shares["alice"]
    assert set(shares) == {"alice", "bob", "charlie"}
    with pytest.raises(ValueError):
        percentage_split(101, {"alice": 33.33, "bob": 33.33, "charlie": 33.33})


def test_decimal_conversion_does_not_silently_round_user_amounts():
    assert to_cents(0.01) == 1
    assert to_cents(12.5) == 1250
    for invalid in (0, -1, 12.345, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            to_cents(invalid)


def test_debt_plan_transfers_reconcile_each_members_net_position():
    balances = [
        {"user_id": "alice", "name": "Alice", "balance_cents": 10001},
        {"user_id": "bob", "name": "Bob", "balance_cents": -7000},
        {"user_id": "charlie", "name": "Charlie", "balance_cents": -3001},
    ]
    remaining = {row["user_id"]: row["balance_cents"] for row in balances}
    transfers = debt_plan(balances)
    for transfer in transfers:
        assert transfer["amount_cents"] > 0
        remaining[transfer["from_id"]] += transfer["amount_cents"]
        remaining[transfer["to_id"]] -= transfer["amount_cents"]
    assert all(value == 0 for value in remaining.values())
