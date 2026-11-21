"""Tests for plan table rendering."""

import pytest
import pytest_bazel
from pydantic import BaseModel

from gmail_archiver.plan import Action, PlannedAction
from gmail_archiver.plan_display import _collect_display_columns
from gmail_archiver.planners.aliexpress import AliExpressEmail, AliExpressStatus
from gmail_archiver.planners.doordash import DoorDashEmail, DoorDashEmailType
from gmail_archiver.planners.square import SquareReceipt


@pytest.mark.parametrize(
    ("custom_data", "columns", "hide_subject"),
    [
        (SquareReceipt(), [("amount", "Amount"), ("merchant_name", "Merchant")], False),
        (
            DoorDashEmail(restaurant=None, email_type=DoorDashEmailType.UNKNOWN),
            [("restaurant", "Restaurant"), ("email_type", "Type")],
            False,
        ),
        (
            AliExpressEmail(order_id=None, status=AliExpressStatus.CONFIRMED),
            [("order_id", "Order ID"), ("status", "Status")],
            True,
        ),
    ],
)
def test_each_planners_columns_reach_the_plan_table(
    custom_data: BaseModel, columns: list[tuple[str, str]], hide_subject: bool
):
    planned = PlannedAction(planner_name="test-planner", action=Action(custom_data=custom_data))

    assert _collect_display_columns([planned]) == (columns, hide_subject)


if __name__ == "__main__":
    pytest_bazel.main()
