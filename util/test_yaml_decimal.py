from datetime import date
from decimal import Decimal

import pytest
import pytest_bazel
import yaml

from util.yaml_decimal import DecimalSafeLoader


def test_yaml_decimals_preserve_numeric_tokens_and_other_scalar_types() -> None:
    document = yaml.load(
        """amount: 9007199254740993.01
rate: 0.10000000000000000000000000001
negative: -1_234.56
scientific: 1.234567890123456789e+20
count: 2
enabled: true
date: 2026-10-27
label: plain text
""",
        Loader=DecimalSafeLoader,
    )
    assert document == {
        "amount": Decimal("9007199254740993.01"),
        "rate": Decimal("0.10000000000000000000000000001"),
        "negative": Decimal("-1234.56"),
        "scientific": Decimal("1.234567890123456789e+20"),
        "count": 2,
        "enabled": True,
        "date": date(2026, 10, 27),
        "label": "plain text",
    }


@pytest.mark.parametrize("value", [".nan", ".inf", "-.Inf"])
def test_nonfinite_yaml_scalars_remain_decimals_for_model_validation(value: str) -> None:
    result = yaml.load(value, Loader=DecimalSafeLoader)
    assert isinstance(result, Decimal)
    assert not result.is_finite()


def test_decimal_loader_keeps_safe_loader_constructor_restrictions() -> None:
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.load("!!python/object:builtins.object {}", Loader=DecimalSafeLoader)


if __name__ == "__main__":
    pytest_bazel.main()
