import sys

from inventree.api import InvenTreeAPI
from inventree.base import ParameterTemplate
from inventree.part import Part as InvPart
from inventree.stock import StockItem
from tqdm import tqdm

from inventree_utils.beautifier.config import api_from_config
from inventree_utils.beautifier.inventree_util import create_part_parameter, list_part_parameters, part_url
from inventree_utils.samplebooks_import.samplebooks_parts_data import BookPart, Resistor, parts

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


def format_value(part: BookPart) -> str:
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


def get_part_category(part: BookPart) -> int:
    return RESISTOR_CATEGORY_ID if isinstance(part, Resistor) else CAPACITOR_CATEGORY_ID


def get_location_id(part: BookPart) -> int:
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


def get_quantity(part: BookPart) -> int:
    """
    Apply your custom count rules.
    """
    if isinstance(part, Resistor):
        val_ohms = part.resistance.to("ohm").magnitude

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
    val_pf = part.capacitance.to("pF").magnitude

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


def build_part_name(p: BookPart) -> str:
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


def build_part_description(p: BookPart) -> str:
    """
    Append an auto-gen marker to the user-supplied description (if any).
    """
    base = p.description
    marker = "(bulk SMD sample book import)"
    if base:
        return f"{base} {marker}"
    return marker


def part_parameters(p: BookPart) -> dict[str, str]:
    values = {PACKAGE_PT_NAME: f"SMD {p.package}"}
    value_template = RESISTANCE_PT_NAME if isinstance(p, Resistor) else CAPACITANCE_PT_NAME
    values[value_template] = format_value(p)
    if p.tolerance:
        values[TOLERANCE_PT_NAME] = p.tolerance
    if not isinstance(p, Resistor):
        if p.dielectric:
            values[DIELECTRIC_PT_NAME] = p.dielectric
        if p.voltage_rating is not None:
            values[VOLTAGE_PT_NAME] = f"{p.voltage_rating:g} V"
    return values


def part_matches_in_db(invpart: InvPart, p: BookPart, param_templates: dict[str, ParameterTemplate]) -> bool:
    """Compare only the fields owned by the importer. Operators can edit descriptions and add parameters."""
    differences = []

    def diff(label: str, actual: str | int, expected: str | int):
        if str(actual) != str(expected):
            differences.append(f"{label} differs: InvenTree='{actual}' vs Script='{expected}'")

    diff("Name", invpart.name, build_part_name(p))
    diff("Category", invpart.category, get_part_category(p))
    actual_values = {row["template"]: str(row["data"]).strip() for row in invpart.parameters}
    for name, expected in part_parameters(p).items():
        diff(f"[{name}]", actual_values.get(param_templates[name].pk, ""), expected)

    if not differences:
        return True

    print(f"Part '{invpart.name}' mismatch details:")
    for difference in differences:
        print(f"  - {difference}")
    return False


def create_part_in_inventree(api: InvenTreeAPI, p: BookPart, param_templates: dict[str, ParameterTemplate]) -> InvPart:
    parameters = part_parameters(p)
    for name, value in parameters.items():
        choices = param_templates[name].choices
        if choices and value not in choices.split(","):
            raise ValueError(f"{value!r} not in {choices}")

    data = {
        "name": build_part_name(p),
        "category": get_part_category(p),
        "description": build_part_description(p),
        # Store the "autogen" marker in keywords as well.
        "keywords": ANCHOR,
    }
    newp: InvPart | None = InvPart.create(api, data)
    if newp is None:
        raise RuntimeError(f"InvenTree returned no part for {data['name']}")
    # A category may supply initial parameters when the part is created.
    existing = {row.template: row for row in list_part_parameters(api, model_id=newp.pk)}
    for name, value in parameters.items():
        template = param_templates[name]
        if template.pk in existing:
            existing[template.pk].save({"data": value})
        else:
            create_part_parameter(api, newp, template, value)
    return newp


def create_stock_for_part(api: InvenTreeAPI, prt: InvPart, p: BookPart):
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
    api = api_from_config()
    needed_templates = [
        RESISTANCE_PT_NAME,
        CAPACITANCE_PT_NAME,
        TOLERANCE_PT_NAME,
        DIELECTRIC_PT_NAME,
        VOLTAGE_PT_NAME,
        PACKAGE_PT_NAME,
    ]
    all_templates = ParameterTemplate.list(api, raise_error=True)
    tmpl_map = {t.name: t for t in all_templates}
    for nt in needed_templates:
        if nt not in tmpl_map:
            raise RuntimeError(f"Missing ParameterTemplate '{nt}' in InvenTree. Please create it first.")

    # Check every existing name before offering to create a part, including manually entered parts.
    existing_map = {part.name: part for part in InvPart.list(api, parameters=True, limit=0, raise_error=True)}

    # Validate every stock quantity before the first write.
    for p in parts:
        get_quantity(p)

    missing_parts = []
    for p in parts:
        name = build_part_name(p)
        if name in existing_map:
            invp = existing_map[name]
            if not part_matches_in_db(invp, p, tmpl_map):
                print(
                    f"WARNING: Part '{name}' already exists but differs from the script's data! {part_url(api, invp)}"
                )
        else:
            missing_parts.append(p)

    if not missing_parts:
        print("No missing parts.")
        return

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

    for mp in tqdm(missing_parts):
        newp = create_part_in_inventree(api, mp, tmpl_map)
        si = create_stock_for_part(api, newp, mp)
        print(f"{newp.name} (N={int(si.quantity)}) created: {part_url(api, newp)}")
        if add_one:
            print("Stopped after adding one part (debug mode).")
            break

    print("Done.")


if __name__ == "__main__":
    main()
