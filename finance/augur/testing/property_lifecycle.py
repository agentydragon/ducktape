"""Shared property-lifecycle input for Augur browser checks."""

import json
from urllib.parse import urlencode

# Buy Location A, rent half at month 24, improve at 60 and sell at 120.
# Keep a bounded real population and an explicit 240-month horizon.
_SCENARIOS = {
    "v": 3,
    "base": {
        "label": "Base",
        "input": {
            "propertyId": "location_a_property",
            "propertyLifecycleEvents": [
                {"kind": "set_rented_fraction", "month": 24, "rentedFractionPct": 50},
                {"kind": "capital_improvement", "month": 60, "amount": 50000},
                {"kind": "property_sale", "month": 120, "closingCostPct": 6},
            ],
        },
    },
    "variants": [],
}
# Exercise real trajectories without making browser interaction checks a throughput gate.
PROPERTY_LIFECYCLE_URL = "/product?" + urlencode({"scenarios": json.dumps(_SCENARIOS), "h": "240", "n": "32"})
