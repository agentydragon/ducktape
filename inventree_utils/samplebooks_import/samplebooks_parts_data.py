"""Bulk-add my sample books & stock from them into Inventree."""

import dataclasses
from itertools import groupby

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
        "description": "Dielectric, voltage rating etc. not listed in sample book",
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
    [330, 470, 680, 820, 1_000, 1_500, 2_200, 3_300, 4_700, 6_800],
    {"voltage_rating": 25},
    [8_200, 10_000, 15_000, 22_000],
    {"voltage_rating": 16},
    [33_000, 47_000, 68_000, 82_000, 100_000],
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
        "description": "Labeled 'Wa1[dielectric type]'",
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
    [220, 330, 470, 560, 680, 820, 1_000, 1_500, 2_200, 3_300, 4_700, 6_800],
    [8_200, 10_000, 15_000],
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
    {"voltage_rating": 10},
    [4_700_000, 10_000_000],
)


print(len(parts), "parts")

v = ureg.V


def format_value(part):
    """Format resistance or capacitance values using pint for human-friendly units."""
    if isinstance(part, Resistor):
        value = part.resistance
    elif isinstance(part, Capacitor):
        value = part.capacitance
    else:
        raise ValueError(f"Unknown part type: {type(part)}")
    value = value.to_compact()  # type: ignore[assignment]

    # Extract the numeric magnitude and round to 3 significant figures
    rounded_value = round(value.magnitude, 3)

    # Reconstruct the Quantity with rounded magnitude
    return f"{rounded_value:g} {value.units:~P}"
    ###return f"{value.to_compact():~P}"  # Auto-scale to nF, µF ...


def part_key(part):
    return (part.package, type(part).__name__)


def value_key(part):
    return part.resistance if isinstance(part, Resistor) else part.capacitance


# Sort parts by (package, type) first, then by value
parts.sort(key=lambda p: (p.package, type(p).__name__, value_key(p)))


def eyeball_check():
    for (package, part_type), group_iter in groupby(parts, key=part_key):
        group = list(group_iter)  # Convert to list for multiple iterations

        # Determine available fields dynamically
        has_tolerance = any(hasattr(p, "tolerance") and p.tolerance is not None for p in group)
        has_voltage = any(hasattr(p, "voltage_rating") and p.voltage_rating is not None for p in group)
        has_dielectric = any(hasattr(p, "dielectric") and p.dielectric is not None for p in group)

        # Generate headers dynamically
        headers = ["Value"]
        if has_tolerance:
            headers.append("Tolerance")
        if has_voltage:
            headers.append("Voltage")
        if has_dielectric:
            headers.append("Dielectric")
        headers.append("Changes")  # Always include changes column

        # Generate header format string dynamically
        col_widths = [15, 12, 12, 15, 20]  # Default column widths
        col_widths = col_widths[: len(headers)]  # Adjust width based on active columns
        header_format = " ".join(f"{{:<{w}}}" for w in col_widths)
        divider = "-" * sum(col_widths)

        # Print table headers
        print(f"\n### {package} {part_type}s ###")
        print(header_format.format(*headers))
        print(divider)

        last_tolerance = None
        last_voltage = None
        last_dielectric = None
        last_description = None

        for part in sorted(group, key=value_key):
            value = format_value(part)
            changes = []

            if has_tolerance and part.tolerance != last_tolerance:
                changes.append(f"tolerance={part.tolerance}")
                last_tolerance = part.tolerance

            if has_voltage and isinstance(part, Capacitor) and part.voltage_rating != last_voltage:
                changes.append(f"voltage={part.voltage_rating}V")
                last_voltage = part.voltage_rating

            if has_dielectric and isinstance(part, Capacitor) and part.dielectric != last_dielectric:
                changes.append(f"dielectric={part.dielectric}")
                last_dielectric = part.dielectric

            if part.description != last_description:
                changes.append(f"description={part.description}")
                last_description = part.description

            # Prepare row data dynamically
            row_data = [value]
            if has_tolerance:
                row_data.append(part.tolerance or "-")
            if has_voltage:
                row_data.append(
                    str(part.voltage_rating) + "V" if isinstance(part, Capacitor) and part.voltage_rating else "-"
                )
            if has_dielectric:
                row_data.append(part.dielectric if isinstance(part, Capacitor) and part.dielectric else "-")
            row_data.append(" | ".join(changes) if changes else "")

            print(header_format.format(*row_data))


### eyeball_check()


# next i want to add those parts to inventree.
# use the python inventree client library.
#
# i have cateogires named 'Resistors' and 'Capacitors', use them.
# you can assume capacitors category ID is 12, resistors category ID is 8.
# i have 'Resistance' and 'Capacitance' parameters, put into them the nicely
# formatted value, e.g. "4 nF". use 'Tolerance' parameter, put into it the
# tolerance string i have. if 'Description' is set & nonempty, put it into
# part description. also pick a marker string that this part is auto-generated
# by this script, and put it into the part. at least put it into description
# (human readable form of it), but if you know some better field to put it,
# please do.
# also set the "Dielectric" parameter for capacitors. of course do not set
# parameter values if they aren't set in this script. Also use "Voltage rating"
# parameter, put in string like "5 V".
# Set 'Package' field. it needs to have a prefix -- for 0402 part, it should
# be set to "SMD 0402".
# create a stock item for every part you add. set its location to the
# corresponding LOC_* location id, e.g. LOC_0201 = 1 for 0201 parts.
# set the quantity for parts according to these rules:
# R0201: everything has count 50, except 200R, 220R - those are 35
# C0201: everything has count 50, except 100nF - those are 40
# R0402: everything has count 50, except 10R, 10k - those are 40
# C0402: all 50, except quantity of 100 nF is 130
# R0603: for (0, 1k, 10k) count is 100. everything else under 100k (including
#        100k) has count 50. everything strictly higher than 100k has count 25
# C0603: everything has count 25
# C0805: everything has count 50
#
# all the parameters i mentioned above will already exist. crash if you can't
# find them.
#
# precompute part counts in advance before staritng database inserts; make
# sure all rules i applied above applied, i.e. if i tell you "count of 123 ohm
# should be 50" and there is no such resistor, you should crash.
#
# before insert, check in inventree if i already have the given part.
#
# create a main. first find all existing parts generated by this script.
# if in same state as this script would make them, skip them. if they exist
# but differ, raise error. if any don't exist, ask for confirmation whether
# i wanna start adding - give me options yes-all-all / yes but add just one
# (for debvugging) / no abort. when you add a part, give me a link to it.
#
# some potentially helpful snippets i grabbed from inventree-python tests/code,
# in case you might not be familiar:
#
#    prt = Part.create(self.api, {
#        "category": cat.pk,
#        "name": name,
#        "description": "A new part in this category",
#    })
#
# existingTemplates = len(ParameterTemplate.list(self.api))
#
# param = Parameter.create(self.api, data={'part': p.pk, 'template': parametertemplate.pk, 'data': 10})

E24 = [1, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2, 2.2, 2.4, 2.7, 3, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1]


# Duplicate class definitions (Part/Resistor/Capacitor) removed; see top-level BasePart/Resistor/Capacitor
# Duplicate function definitions (crunch, _r, _c) also removed; see top-level definitions

parts = []

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

v = ureg.V


# Sort parts by (package, type) first, then by value
parts.sort(key=lambda p: (p.package, type(p).__name__, value_key(p)))

# for (package, part_type), group in groupby(parts, key=part_key):
#    group = list(group)  # Convert to list for multiple iterations
#
#    # Determine available fields dynamically
#    has_tolerance = any(
#        hasattr(p, "tolerance") and p.tolerance is not None for p in group
#    )
#    has_voltage = any(
#        hasattr(p, "voltage_rating") and p.voltage_rating is not None for p in group
#    )
#    has_dielectric = any(
#        hasattr(p, "dielectric") and p.dielectric is not None for p in group
#    )
#
#    # Generate headers dynamically
#    headers = ["Value"]
#    if has_tolerance:
#        headers.append("Tolerance")
#    if has_voltage:
#        headers.append("Voltage")
#    if has_dielectric:
#        headers.append("Dielectric")
#    headers.append("Changes")  # Always include changes column
#
#    # Generate header format string dynamically
#    col_widths = [15, 12, 12, 15, 20]  # Default column widths
#    col_widths = col_widths[: len(headers)]  # Adjust width based on active columns
#    header_format = " ".join(f"{{:<{w}}}" for w in col_widths)
#    divider = "-" * sum(col_widths)
#
#    # Print table headers
#    print(f"\n### {package} {part_type}s ###")
#    print(header_format.format(*headers))
#    print(divider)
#
#    last_tolerance = None
#    last_voltage = None
#    last_dielectric = None
#    last_description = None
#
#    for part in sorted(group, key=value_key):
#        value = format_value(part)
#        changes = []
#
#        if has_tolerance and part.tolerance != last_tolerance:
#            changes.append(f"tolerance={part.tolerance}")
#            last_tolerance = part.tolerance
#
#        if has_voltage and part.voltage_rating != last_voltage:
#            changes.append(f"voltage={part.voltage_rating}V")
#            last_voltage = part.voltage_rating
#
#        if has_dielectric and part.dielectric != last_dielectric:
#            changes.append(f"dielectric={part.dielectric}")
#            last_dielectric = part.dielectric
#
#        if part.description != last_description:
#            changes.append(f"description={part.description}")
#            last_description = part.description
#
#        # Prepare row data dynamically
#        row_data = [value]
#        if has_tolerance:
#            row_data.append(part.tolerance or "-")
#        if has_voltage:
#            row_data.append(
#                str(part.voltage_rating) + "V" if part.voltage_rating else "-"
#            )
#        if has_dielectric:
#            row_data.append(part.dielectric or "-")
#        row_data.append(" | ".join(changes) if changes else "")
#
#        print(header_format.format(*row_data))
