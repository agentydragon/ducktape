from inventree.api import InvenTreeAPI
from inventree.part import Part

from .cli_util import choose
from .inventree_util import part_url
from .lcsc_util import lcsc_product_link, parse_url_for_lcsc_id


def normalize_url(url: str) -> str:
    """
    If url is a recognized LCSC link, rewrite it to short form:
      e.g. https://www.lcsc.com/product-detail/C12345.html
    Otherwise returns the original url.
    """
    lcsc_id = parse_url_for_lcsc_id(url)
    if lcsc_id:
        return lcsc_product_link(lcsc_id)
    return url


def fix_lcsc_links(api: InvenTreeAPI):
    """
    Loops through all parts, finds "long" LCSC product-detail URLs in `Part.link`,
    rewrites them to the short format, and interactively asks for confirmation
    on each update.
    """
    parts = Part.list(api)  # Let it raise if error
    apply_all = False

    for p in parts:
        if not p.link:
            continue

        new_link = normalize_url(p.link)
        if new_link == p.link:
            continue

        print(f"\n{part_url(api, p)} {p.name}")
        print(f"Change link:\n    {p.link}\n  → {new_link}?")

        if not apply_all:
            choice = choose("Proceed?", ["y", "a", "q"])
            if choice == "q":
                print("Quitting script.")
                return
            if choice == "a":
                apply_all = True
            elif choice != "y":
                continue

        p.save(data={"link": new_link})
        print("Link updated.")
