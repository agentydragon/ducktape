"""Compare Hakyll and Zola static site outputs with DOM-aware HTML normalization."""

import difflib
import filecmp
import sys
from pathlib import Path

import html5lib


def hakyll_to_zola_path(rel: str) -> str | None:
    """Map a Hakyll relative path to the expected Zola relative path.

    Returns None for files that should be skipped (source files not in output).
    """
    match rel:
        case "about.html":
            return "about/index.html"
        case "found.html":
            return "found/index.html"
        case "nfc.html":
            return "nfc/index.html"
        case "nfc-armband.html":
            return "nfc-armband/index.html"
        case "archive.html":
            return "archive/index.html"
        case "index.html" | "atom.xml" | "rss.xml" | "robots.txt" | "sitemap.xml":
            return rel
        case "css/default.css":
            return "default.css"
        case "css/default.scss":
            return None
        case _ if rel.startswith("posts/") and rel.endswith(".html"):
            return rel.removesuffix(".html") + "/index.html"
        case _:
            return rel


def normalize_html(content: str) -> str:
    """Parse HTML with html5lib (WHATWG spec) and re-serialize canonically."""
    doc = html5lib.parse(content, treebuilder="etree", namespaceHTMLElements=False)
    return html5lib.serialize(
        doc, tree="etree", quote_attr_values="always", omit_optional_tags=False, alphabetical_attributes=True
    )


RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
NC = "\033[0m"


def compare_text(hakyll_path: Path, zola_path: Path, hakyll_rel: str, zola_rel: str, normalize: bool = False) -> bool:
    """Compare two text files. Returns True if identical."""
    hakyll_text = hakyll_path.read_text()
    zola_text = zola_path.read_text()

    if normalize:
        hakyll_text = normalize_html(hakyll_text)
        zola_text = normalize_html(zola_text)
        # Strip indentation — html5lib normalizes structure but indentation
        # still differs (Hakyll uses tabs, Zola uses spaces).
        hakyll_text = "\n".join(line.strip() for line in hakyll_text.splitlines())
        zola_text = "\n".join(line.strip() for line in zola_text.splitlines())

    hakyll_lines = hakyll_text.splitlines(keepends=True)
    zola_lines = zola_text.splitlines(keepends=True)

    if hakyll_lines == zola_lines:
        return True

    diff = list(
        difflib.unified_diff(hakyll_lines, zola_lines, fromfile=f"hakyll/{hakyll_rel}", tofile=f"zola/{zola_rel}")
    )
    for line in diff[:80]:
        sys.stdout.write(line)
    if len(diff) > 80:
        print(f"  ... ({len(diff) - 80} more diff lines)")
    print()
    return False


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <hakyll_dir> <zola_dir>", file=sys.stderr)
        sys.exit(2)

    hakyll_dir = Path(sys.argv[1])
    zola_dir = Path(sys.argv[2])

    identical = 0
    differing = 0
    hakyll_only = 0
    zola_only = 0

    print(f"{BOLD}=== Site Diff: Hakyll vs Zola ==={NC}")
    print(f"Hakyll: {hakyll_dir}")
    print(f"Zola:   {zola_dir}")
    print()
    print(f"{BOLD}--- Hakyll files ---{NC}")

    expected_zola_paths: set[str] = set()

    for hakyll_file in sorted(hakyll_dir.rglob("*")):
        if not hakyll_file.is_file():
            continue

        rel = str(hakyll_file.relative_to(hakyll_dir))
        zola_rel = hakyll_to_zola_path(rel)

        if zola_rel is None:
            continue

        expected_zola_paths.add(zola_rel)
        zola_file = zola_dir / zola_rel

        if not zola_file.exists():
            print(f"  {YELLOW}HAKYLL-ONLY{NC}  {rel}  (expected: {zola_rel})")
            hakyll_only += 1
            continue

        suffix = hakyll_file.suffix
        if suffix == ".html":
            if compare_text(hakyll_file, zola_file, rel, zola_rel, normalize=True):
                print(f"  {GREEN}IDENTICAL{NC}    {rel}")
                identical += 1
            else:
                print(f"  {RED}DIFFERENT{NC}    {rel}  <->  {zola_rel}")
                differing += 1
        elif suffix in {".xml", ".css", ".txt"}:
            if compare_text(hakyll_file, zola_file, rel, zola_rel):
                print(f"  {GREEN}IDENTICAL{NC}    {rel}")
                identical += 1
            else:
                print(f"  {RED}DIFFERENT{NC}    {rel}  <->  {zola_rel}")
                differing += 1
        elif filecmp.cmp(hakyll_file, zola_file, shallow=False):
            print(f"  {GREEN}IDENTICAL{NC}    {rel}")
            identical += 1
        else:
            print(f"  {RED}DIFFERENT{NC}    {rel}  <->  {zola_rel}  (binary)")
            differing += 1

    print()
    print(f"{BOLD}--- Zola-only files (not in Hakyll) ---{NC}")

    for zola_file in sorted(zola_dir.rglob("*")):
        if not zola_file.is_file():
            continue
        rel = str(zola_file.relative_to(zola_dir))
        if rel not in expected_zola_paths:
            print(f"  {CYAN}ZOLA-ONLY{NC}    {rel}")
            zola_only += 1

    print()
    print(f"{BOLD}=== Summary ==={NC}")
    print(f"  {GREEN}Identical{NC}:    {identical}")
    print(f"  {RED}Different{NC}:    {differing}")
    print(f"  {YELLOW}Hakyll-only{NC}:  {hakyll_only}")
    print(f"  {CYAN}Zola-only{NC}:    {zola_only}")


if __name__ == "__main__":
    main()
