"""Bulk-add my sample books & stock from them into Inventree."""

import dataclasses

import pint

E24 = [1, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2, 2.2, 2.4, 2.7, 3, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1]
assert len(E24) == 24


@dataclasses.dataclass
class BasePart:
    tolerance: str | None
    package: str
    description: str | None


@dataclasses.dataclass
class Resistor(BasePart):
    resistance: pint.Quantity


@dataclasses.dataclass
class Capacitor(BasePart):
    capacitance: pint.Quantity
    dielectric: str
    voltage_rating: float


def crunch(factory, *items):
    # each item is either value, or setter.
    result: list[BasePart] = []
    state = {}
    for item in items:
        if isinstance(item, dict):
            state.update(item)
        elif isinstance(item, list):
            assert all(isinstance(i, int | float) for i in item)
            parts.extend(factory(**state, value=i) for i in item)
        else:
            assert isinstance(item, int | float), item
            parts.append(factory(**state, value=item))
    return result


ureg: pint.UnitRegistry = pint.UnitRegistry()
ohm = ureg.ohm
pf = ureg.pF


def _r(value, **kwargs):
    return Resistor(resistance=value * ohm, **kwargs)


def _c(value, **kwargs):
    return Capacitor(capacitance=value * pf, **kwargs)


parts: list[BasePart] = []

# R 0201
crunch(
    _r,
    {"package": "0201", "tolerance": "1%", "description": "Yageo FR-07 series"},
    0,
    [i * 10**decade for decade in [0, 1, 2, 3, 4, 5, 6] for i in E24],
    1e7,
)

# C 0201
crunch(
    _c,
    # unfortunately lotsa stuff unknown
    {
        "package": "0201",
        "voltage_rating": None,
        "dielectric": None,
        "tolerance": None,
        "description": "dielectric, voltage rating etc. unknown, not listed by seller",
    },
    0.5,
    [1, 1.5, 2, 2.2, 2.7, 3, 3.3, 3.9, 4, 4.7, 5.6, 7, 8, 8.2, 9],
    [10, 12, 15, 18, 20, 22, 27, 33, 39, 47, 56, 68, 82],
    [100, 120, 150, 180, 220, 330, 470, 680, 820],
    # nf
    [1000, 1800, 2200, 3300, 3900, 4700, 5600, 6800, 10000, 22000],
    [33_000, 100_000, 220_000],
)

# R 0402
crunch(
    _r,
    {"package": "0402", "tolerance": "1%", "description": None},
    0,
    [i * 10**decade for decade in [0, 1, 2, 3, 4, 5, 6] for i in E24],
    1e7,
)

# C 0402
crunch(
    _c,
    {"package": "0402", "voltage_rating": 50, "dielectric": "COG", "tolerance": "0.1pF", "description": None},
    0.5,
    {"tolerance": "0.25pF"},
    [0.7, 1, 1.2, 1.3, 1.5, 1.6, 1.8, 2, 2.2, 2.4, 2.5, 2.7, 3, 3.3, 3.6],
    [3.9, 4.3, 4.7, 5],
    {"tolerance": "0.5pF"},
    [6, 6.2, 6.8, 7, 7.5, 8, 8.2, 9],
    {"tolerance": "5%"},
    [10, 11, 12, 15, 16, 18, 20, 22, 24, 27, 30, 33, 36, 39, 43, 47, 51, 56],
    [62, 68, 82],  # for some reason no 75, 91
    [100, 110, 120, 150, 180],
    {"dielectric": "X7R"},
    [200, 220],
    {"tolerance": "10%"},
    [330, 470, 680, 820, 1000, 1500, 2200, 3300, 4700, 6800],
    {"voltage_rating": 25},
    [8200, 10000, 15000, 22000],
    {"voltage_rating": 16},
    [33000, 47000, 68000, 82000, 100_000],
    {"dielectric": "X5R", "voltage_rating": 10},
    [220_000, 470_000, 680_000, 820_000, 1_000_000],
)

# R 0603
crunch(
    _r,
    {"package": "0603", "tolerance": "1%", "description": 'Labeled "YAGFJR-07"'},
    0,
    [i * 10**decade for decade in [0, 1, 2, 3, 4, 5] for i in [1, 1.2, 1.5, 2, 2.7, 3.3, 4.3, 5.1, 6.8, 8.2]],
    [1e6, 2e6, 3.3e6, 4.7e6, 1e7],
)

# C 0603
crunch(
    _c,
    {
        "package": "0603",
        "voltage_rating": 50,
        "dielectric": "NPO",
        "tolerance": "0.25pF",
        "description": "Labeled 'Wa1(dielectric type)'",
    },
    1,
    {"tolerance": "0.5pF"},
    5,
    {"tolerance": "5%"},
    [10, 15, 22, 27, 33, 47, 68, 82, 100, 120, 150, 180],
    {"dielectric": "X7R", "voltage_rating": 50, "tolerance": "10%"},
    [220, 470, 680, 1000, 10_000, 100_000, 470_000],
    {"dielectric": "X5R", "voltage_rating": 25},
    1_000_000,
    {"dielectric": "X5R", "voltage_rating": 16},
    2_200_000,
)

# C 0805
crunch(
    _c,
    {"package": "0805", "voltage_rating": 50, "dielectric": "COG", "tolerance": "0.25pF", "description": None},
    [0.5, 0.75, 1, 1.1, 1.2, 1.3, 1.5, 1.8, 2, 2.2, 2.4, 2.5, 2.7, 3, 3.3],
    [3.6, 3.9, 4, 4.3, 4.7, 5],
    {"tolerance": "0.5pF"},
    [5.1, 5.6, 6, 6.2, 6.8, 7, 7.5, 8, 8.2, 9, 9.1],
    {"tolerance": "5%"},
    [10, 11, 12, 13, 15, 16, 18, 20, 22, 24, 27, 30, 33, 36, 39, 43, 47, 51],
    [56, 62, 68, 75, 82, 91, 100, 110, 120, 130, 150, 160, 180, 200],
    {"dielectric": "X7R", "tolerance": "10%"},
    [220, 330, 470, 560, 680, 820, 1000, 1500, 2200, 3300, 4700, 6800],
    [8200, 10_000, 15_000],
    {"voltage_rating": 25},
    [22_000, 33_000, 47_000, 68_000, 82_000, 100_000],
    {"voltage_rating": 50},
    [220_000, 470_000],
    {"voltage_rating": 25},
    [680_000],
    {"voltage_rating": 10},
    [1_000_000],
    {"voltage_rating": 16, "dielectric": "X5R"},
    [2_200_000],
    {"voltage_rating": 10, "dielectric": "X5R"},
    [4_700_000, 10_000_000],
)

print(len(parts), "parts")


def value_key(part):
    return part.resistance if isinstance(part, Resistor) else part.capacitance


# Sort parts by (package, type) first, then by value
parts.sort(key=lambda p: (p.package, type(p).__name__, value_key(p)))
