import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from textwrap import dedent

from inventree.api import InvenTreeAPI
from inventree.base import Parameter, ParameterTemplate
from inventree.part import Part
from tqdm import tqdm

from inventree_utils.beautifier.cli_util import build_table, choose
from inventree_utils.beautifier.inventree_util import part_url
from inventree_utils.beautifier.iter_util import unwrap_singleton

JELLYBEAN_PARAM_NAME = "Jellybean P/N"


def get_parts_without_parameter(api: InvenTreeAPI, template):
    """Return parts missing the given parameter."""
    part_pks_with_param = {param.part for param in Parameter.list(api, template=template.pk)}
    return [p for p in Part.list(api) if p.pk not in part_pks_with_param]


def build_editor_file(api, edited_parts):
    def _propose_jellybean(part):
        """
        Naive guess for a "jellybean" P/N. e.g.: "74HC164, SOIC-14" -> "74HC164"
        """
        proposed = part.name.split()[0][:20].strip(",")
        return proposed if proposed else "JELLYBEAN"

    rows = [(part_url(api, part), _propose_jellybean(part), part.name) for part in edited_parts]
    table = build_table(rows)
    return (
        dedent(
            """
        # Uncomment and edit lines to assign jellybean part numbers.
        # Format:
        #   <part link>  <jellybean part number>  <part name>
        #
        # Do not change part links or names - the script will fail.
        # Lines starting with '#' will be ignored.
        # -------------------------------------------------------------
        """
        )
        + "\n"
        + "\n".join("# " + r for r in table)
        + "\n"
    )


class UserSyntaxError(Exception):
    pass


def parse_file_lines(lines):
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Split by consecutive whitespace into columns. Part link
        # and jellybean part number can't have whitespace, so this should
        # work.
        tokens = re.split(r"\s+", line, maxsplit=2)
        if len(tokens) < 3:
            raise UserSyntaxError(f"Line has too few tokens: {line!r}")

        yield tokens


def parse_jellybean_editor_file(part_lookup, file_path):
    """
    Parse the user-edited file. Return an iterable of (Part, jellybeanPN).
    Raise UserSyntaxError if format is invalid or there's a mismatch.
    """
    with Path(file_path).open() as f:
        parsed = list(parse_file_lines(f))

    for url, jellybean_pn, part_name in parsed:
        if not (part := part_lookup.get(url)):
            raise UserSyntaxError(f"Unknown part URL: {url}")

        # Let's ensure the line ends with the correct part name
        if part_name != part.name:
            raise UserSyntaxError(f"Part name mismatch: {part_name=} !={part.name=}")

        if not jellybean_pn:
            raise UserSyntaxError(f"Invalid jellybean P/N '{jellybean_pn}'")

        yield (part, jellybean_pn)


def _run_editor(file):
    cmdline = (os.environ.get("EDITOR", "vi"), file)
    print(f"Opening editor: {shlex.join(cmdline)}")
    return subprocess.run(cmdline, check=False).returncode == 0


def build_assignments_table(api, assignments):
    """
    Build a table for the final user confirmation of assignments.
    """
    rows = []
    for part, jellypn in assignments:
        rows.append((part_url(api, part), part.name, "→", jellypn))
    return build_table(rows)


def elicit_assignments(api, file_path, part_lookup):
    """Loop: open editor, parse user changes, ask for confirmation or re-edit."""
    while True:
        assignments = None

        if not _run_editor(file_path):
            # editor exited with nonzero exit code
            print("Editor exited unsuccessfully. Aborting.")
            return None

        try:
            assignments = list(parse_jellybean_editor_file(part_lookup, file_path))
        except UserSyntaxError as e:
            print(f"Syntax error: {e}")
            # edit/abort choice handled below

        if assignments is not None and len(assignments) == 0:
            print("No assignments commanded.")

        if assignments:
            print("\nProposed assignments:\n")
            print("\n".join(build_assignments_table(api, assignments)))
            choice = choose("[e]dit / [c]ommit / [a]bort?", ["e", "c", "a"])
        else:
            choice = choose("[e]dit / [a]bort?", ["e", "a"])

        if choice == "a":
            return None
        if choice == "c":
            return assignments


def assign_jellybean(api: InvenTreeAPI):
    """
    Implementation for the "assign-jellybean" command.
    """
    # Find the parameter template.
    param_template = unwrap_singleton(ParameterTemplate.list(api, name=JELLYBEAN_PARAM_NAME))
    edited_parts = get_parts_without_parameter(api, param_template)
    if not edited_parts:
        print(f"All parts already have {param_template.name}.")
        return

    # Write to temp file
    fd, file_path_str = tempfile.mkstemp(prefix="inventree_edit_", suffix=".txt")
    os.close(fd)
    file_path = Path(file_path_str)
    with file_path.open("w") as f:
        f.write(build_editor_file(api, edited_parts))

    part_lookup = {part_url(api, p): p for p in edited_parts}
    assignments = elicit_assignments(api, file_path, part_lookup)
    if not assignments:
        print("Aborting.")
        return
    print("Committing.")

    # commit changes
    progress = tqdm(assignments)
    for part, jellybean_pn in progress:
        Parameter.create(api, data={"part": part.pk, "template": param_template.pk, "data": jellybean_pn})
        progress.set_description(f"{part.name} ← {jellybean_pn}")

    print("Done.")
