from inventree_utils.beautifier.iter_util import unwrap_singleton


def build_table(rows, header=None):
    """
    Builds a simple space-aligned table from rows, optionally with a header row.
    """
    if header:
        rows = [header, *rows[:]]
    column_count = unwrap_singleton({len(row) for row in rows})
    column_widths = [max(len(row[i]) for row in rows) for i in range(column_count)]

    lines = [" ".join(cell.ljust(width) for cell, width in zip(row, column_widths, strict=False)) for row in rows]
    if header:
        lines.insert(1, "-" * max(len(line) for line in lines))
    return lines


def choose(prompt, options):
    """
    Presents a prompt with possible [options]. Loops until user picks one.
    Returns the user choice (lowercased).
    """
    opts_str = "/".join(options)
    while True:
        choice = input(f"{prompt} [{opts_str}] ").strip().lower()
        if choice in options:
            return choice
        print(f"Pick one of {options}.")
