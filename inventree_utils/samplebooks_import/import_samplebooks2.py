#!/usr/bin/env python3

import sys
from collections import defaultdict

import pint
from inventree.api import InvenTreeAPI
from inventree.base import Parameter, ParameterTemplate
from inventree.part import Part as InvPart
from inventree.stock import StockItem
from samplebooks_parts_data import BasePart, Resistor, parts
from tqdm import tqdm

# ---------- Setup / config ----------

SERVER_ADDRESS = "https://inventree.agentydragon.com"
MY_USERNAME = "root"
MY_PASSWORD = "uYee4Nah"

api = InvenTreeAPI(SERVER_ADDRESS, username=MY_USERNAME, password=MY_PASSWORD)

RESISTOR_CATEGORY_ID = 8
CAPACITOR_CATEGORY_ID = 12

# Parameter template names that must already exist:
RESISTANCE_PT_NAME = "Resistance"
CAPACITANCE_PT_NAME = "Capacitance"
TOLERANCE_PT_NAME = "Tolerance"
DIELECTRIC_PT_NAME = "Dielectric"
VOLTAGE_PT_NAME = "Voltage rating"
PACKAGE_PT_NAME = "Package"

ANCHOR = "smd_book_import"

# Stock location IDs
LOC_0201 = 1
LOC_0402 = 2
LOC_0603 = 3
LOC_0805 = 4

# ---------- Data classes ----------

ureg = pint.UnitRegistry()
ohm = ureg.ohm
pf = ureg.pF


# ---------- Parameter helpers ----------


def format_value(part: BasePart) -> str:
    """
    Return the human-friendly quantity string (e.g. "4.7 nF" or "10 kΩ").
    """
    if isinstance(part, Resistor):
        q = part.resistance.to_compact()
    else:
        q = part.capacitance

        # Ensure we don't go below "pF"
        q = q.to("pF") if q.to_compact().to("pF").magnitude < 1 else q.to_compact()

    return f"{round(q.magnitude, 3):g} {q.units:~P}"


def get_package_string(pkg: str) -> str:
    # Prepend "SMD " to the numeric size
    return f"SMD {pkg}"


def get_part_category(part: BasePart) -> int:
    return RESISTOR_CATEGORY_ID if isinstance(part, Resistor) else CAPACITOR_CATEGORY_ID


def get_location_id(part: BasePart) -> int:
    """
    Which stock location to place it in:
      - 0201 => LOC_0201
      - 0402 => LOC_0402
      - 0603 => LOC_0603
      - 0805 => LOC_0805
    """
    pkg = part.package
    match pkg:
        case "0201":
            return LOC_0201
        case "0402":
            return LOC_0402
        case "0603":
            return LOC_0603
        case "0805":
            return LOC_0805
    raise ValueError(f"Unknown package '{pkg}'")


def get_quantity(part: BasePart) -> int:
    """
    Apply your custom count rules.
    """
    if isinstance(part, Resistor):
        val_ohms = part.resistance.to(ohm).magnitude

        if part.package == "0201":
            # R0201
            if val_ohms in (200, 220):
                return 35
            return 50

        if part.package == "0402":
            # R0402
            if val_ohms in (10, 10_000):
                return 40
            return 50

        if part.package == "0603":
            # R0603
            # (0,1k,10k) => 100
            # <=100k => 50
            # >100k => 25
            if val_ohms in (0, 1_000, 10_000):
                return 100
            if val_ohms <= 100_000:
                return 50
            return 25

        raise ValueError(f"No resistor rules for package {part.package}")

    # It's a capacitor
    val_pf = part.capacitance.to(pf).magnitude

    if part.package == "0201":
        # C0201
        # everything 50 except 100nF => 40
        if abs(val_pf - 100_000) < 1e-9:
            return 40
        return 50

    if part.package == "0402":
        # C0402
        # all 50 except 100nF => 130
        if abs(val_pf - 100_000) < 1e-9:
            return 130
        return 50

    if part.package == "0603":
        # C0603 => always 25
        return 25

    if part.package == "0805":
        # C0805 => 50
        return 50

    raise ValueError(f"No capacitor rules for package {part.package}")


# ---------- Building final data ----------


def build_part_name(p: BasePart) -> str:
    """
    E.g. "R 0201 4.7kΩ" or "C 0603 100nF".
    Keep it fairly short, since you'll store details in param fields.
    """
    value = format_value(p).replace(" ", "")
    if p.tolerance:
        value += f"±{p.tolerance}"
    if isinstance(p, Resistor):
        return f"R {p.package} {value}"
    name = f"C {p.package} {value}"
    if p.voltage_rating:
        name += f", {int(p.voltage_rating)} V"
    if p.dielectric:
        name += f", {p.dielectric}"
    return name


def build_part_description(p: BasePart) -> str:
    """
    Append an auto-gen marker to the user-supplied description (if any).
    """
    base = p.description
    marker = "(bulk SMD sample book import)"
    if base:
        return f"{base} {marker}"
    return marker


def part_matches_in_db(
    invpart: InvPart,
    p: BasePart,
    param_templates: dict[str, ParameterTemplate],
    existing_params_for_part: list[Parameter],
) -> bool:
    """
    Compare name, category, description, param fields, etc.
    Return True if it exactly matches what we'd create now.
    Instead of re-querying the server, we use existing_params_for_part,
    which is a pre-fetched list of Parameter objects for invpart.pk.
    """
    differences = []

    def diff(label: str, actual: str | int, expected: str | int | None):
        """Helper: if expected is nonempty and differs from actual, record mismatch."""
        if expected and str(actual) != str(expected):
            differences.append(f"{label} differs: InvenTree='{actual}' vs Script='{expected}'")

    # 1) Check top-level fields
    script_name = build_part_name(p)
    script_cat = get_part_category(p)
    diff("Name", invpart.name, script_name)
    diff("Category", invpart.category, script_cat)
    # diff("Description", invpart.description, build_part_description(p))

    # Build a dict: templateName -> paramValue
    param_dict = {}
    for ep in existing_params_for_part:
        tmpl_id = ep.template
        # find template name

        for name, tmpl in param_templates.items():
            if tmpl.pk == tmpl_id:
                param_dict[name] = str(ep.data).strip()
                break
        # otherwise, if not in param_templates, then don't care about this parameter
        # (it's not one we set)

    # 2) Build a dict of parameter -> expected value
    expected_params = {}
    expected_params[PACKAGE_PT_NAME] = get_package_string(p.package)

    if isinstance(p, Resistor):
        expected_params[RESISTANCE_PT_NAME] = format_value(p)
        if p.tolerance:
            expected_params[TOLERANCE_PT_NAME] = p.tolerance
    else:  # Capacitor
        expected_params[CAPACITANCE_PT_NAME] = format_value(p)
        if p.tolerance:
            expected_params[TOLERANCE_PT_NAME] = p.tolerance
        if p.dielectric:
            expected_params[DIELECTRIC_PT_NAME] = p.dielectric
        if p.voltage_rating is not None:
            expected_params[VOLTAGE_PT_NAME] = f"{p.voltage_rating:g} V"

    # 3) Compare each parameter
    for param_name, script_val in expected_params.items():
        inv_val = param_dict.get(param_name, "")
        diff(f"[{param_name}]", inv_val, script_val)

    if not differences:
        return True

    print(f"Part '{invpart.name}' mismatch details:")
    for difference in differences:
        print(f"  - {difference}")
    return False


def create_part_in_inventree(p: BasePart, param_templates: dict[str, ParameterTemplate]) -> InvPart:
    """
    Actually create the part if not found or mismatch.
    Return the newly created InvenTree Part object.
    """
    data = {
        "name": build_part_name(p),
        "category": get_part_category(p),
        "description": build_part_description(p),
        # Store the "autogen" marker in keywords as well.
        "keywords": ANCHOR,
    }
    newp = InvPart.create(api, data)
    # Set parameters
    pkg_str = get_package_string(p.package)
    set_part_parameter(newp, param_templates[PACKAGE_PT_NAME], pkg_str)

    if isinstance(p, Resistor):
        set_part_parameter(newp, param_templates[RESISTANCE_PT_NAME], format_value(p))
        if p.tolerance:
            set_part_parameter(newp, param_templates[TOLERANCE_PT_NAME], p.tolerance)

    else:  # Capacitor
        set_part_parameter(newp, param_templates[CAPACITANCE_PT_NAME], format_value(p))
        if p.tolerance:
            set_part_parameter(newp, param_templates[TOLERANCE_PT_NAME], p.tolerance)
        if p.dielectric:
            set_part_parameter(newp, param_templates[DIELECTRIC_PT_NAME], p.dielectric)
        if p.voltage_rating is not None:
            volt_str = f"{int(p.voltage_rating)} V"
            set_part_parameter(newp, param_templates[VOLTAGE_PT_NAME], volt_str)

    return newp


def set_part_parameter(prt: InvPart, tmpl: ParameterTemplate, value: str):
    """
    Create (or update) a parameter with the given template and string value.
    """
    # If a parameter with this template already exists, update it; otherwise create a new one
    existing_params = Parameter.list(api, part=prt.pk, template=tmpl.pk)
    if tmpl.choices:
        assert value in tmpl.choices.split(","), f"{value!r} not in {tmpl.choices}"
    if existing_params:
        assert len(existing_params) == 1, f"Multiple parameters with template {tmpl.pk}"
        param_pk = existing_params[0].pk
        param = Parameter(api, param_pk)
        param["data"] = value
        param.save()
    else:
        create_data = {"part": prt.pk, "template": tmpl.pk}
        create_data["data"] = value
        Parameter.create(api, create_data)


def create_stock_for_part(prt: InvPart, p: BasePart):
    """
    Create a stock item with the correct location and quantity.
    """
    loc = get_location_id(p)
    qty = get_quantity(p)
    # status "OK" is 10 by default
    data = {
        "part": prt.pk,
        "location": loc,
        "quantity": qty,
        "status": 10,  # "in stock"
        "notes": "Autogenerated by SMD book import",
    }
    return StockItem.create(api, data)


def prompt_user(prompt_text: str) -> str:
    print(prompt_text, end="")
    sys.stdout.flush()
    return sys.stdin.readline().strip().lower()


def main():
    # 1) Load parameter templates
    needed_templates = [
        RESISTANCE_PT_NAME,
        CAPACITANCE_PT_NAME,
        TOLERANCE_PT_NAME,
        DIELECTRIC_PT_NAME,
        VOLTAGE_PT_NAME,
        PACKAGE_PT_NAME,
    ]
    all_templates = ParameterTemplate.list(api)
    # Map name->template
    tmpl_map = {t.name: t for t in all_templates}
    # Check that each needed template is present
    for nt in needed_templates:
        if nt not in tmpl_map:
            raise RuntimeError(f"Missing ParameterTemplate '{nt}' in InvenTree. Please create it first.")

    # 2) Retrieve all existing "autogen" parts to check what we already have
    #    We'll search by "keywords={ANCHOR}" for quick filtering
    existing_autogen_parts = InvPart.list(api, keyword=ANCHOR, limit=0)  # no limit
    # Convert to dict name->InvPart for quick lookup
    existing_map = {}
    for ep in existing_autogen_parts:
        existing_map[ep.name] = ep

    # Pre-fetch all parameters for parts we already added.
    print("fetching parameters")
    existing_ids = [str(ep.pk) for ep in existing_autogen_parts]
    existing_ids_str = ",".join(existing_ids)
    all_params = Parameter.list(api, part__in=existing_ids_str, limit=0)

    params_by_part = defaultdict(list)
    for param in all_params:
        params_by_part[param.part].append(param)
    print(f"Loaded {len(all_params)} parameters for {len(existing_autogen_parts)} parts")

    # 4) Precompute quantity checks
    #    (Your request: "make sure all rules i said above are satisfied" => crash if mismatch)
    #    For debugging, we'll just do it: we call get_quantity for each to confirm no exceptions.
    for p in parts:
        _ = get_quantity(p)  # Will raise if any weirdness

    # 5) Identify missing or changed parts
    missing_parts = []
    for p in parts:
        name = build_part_name(p)
        if name in existing_map:
            # Check if it matches
            invp = existing_map[name]
            if not part_matches_in_db(invp, p, tmpl_map, params_by_part[invp.pk]):
                print(
                    f"WARNING: Part '{name}' already exists but differs from the script's data! {SERVER_ADDRESS}/part/{invp.pk}/"
                )
            # else it is fine => skip
        else:
            missing_parts.append(p)

    if not missing_parts:
        print("All parts are already present and match script data. Nothing to do.")
        return

    # 6) Interactive prompt
    print(f"\nThe following {len(missing_parts)} parts are missing:")
    for mp in missing_parts:
        print("   ", build_part_name(mp))
    print()
    choice = prompt_user("Add them? [all/no/one] ")
    if choice.startswith("n"):
        print("Aborting.")
        return
    add_one = choice.startswith("o")
    assert choice.startswith("a")

    # 7) Insert missing parts
    for mp in tqdm(missing_parts):
        newp = create_part_in_inventree(mp, tmpl_map)
        si = create_stock_for_part(newp, mp)
        print(f"{newp.name} (N={int(si.quantity)}) created: {SERVER_ADDRESS}/part/{newp.pk}/")
        if add_one:
            print("Stopped after adding one part (debug mode).")
            break

    print("Done.")


if __name__ == "__main__":
    main()
