"""Safe YAML loading with exact decimal scalars for financial configuration."""

from decimal import Decimal

import yaml
from yaml.nodes import ScalarNode


class DecimalSafeLoader(yaml.SafeLoader):
    """Preserve decimal tokens instead of first rounding them to binary floats."""


def _construct_decimal(loader: DecimalSafeLoader, node: ScalarNode) -> Decimal:
    value = loader.construct_scalar(node).replace("_", "").lower()
    return Decimal(value.replace(".inf", "Infinity").replace(".nan", "NaN"))


DecimalSafeLoader.add_constructor("tag:yaml.org,2002:float", _construct_decimal)
