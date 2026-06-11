import tempfile

import requests
import structlog
from bs4 import BeautifulSoup
from inventree.api import InvenTreeAPI
from inventree.company import Company, SupplierPart
from inventree.part import Part

from .cli_util import choose
from .inventree_util import part_url
from .iter_util import unwrap_singleton
from .lcsc_util import lcsc_product_link, parse_url_for_lcsc_id

logger = structlog.get_logger()

LCSC_SCRAPE_HEADERS = {"User-Agent": "Mozilla/5.0"}


def scrape_lcsc_image_url(lcsc_id: str) -> str:
    """
    Scrapes the LCSC product page to find its main (og:image) URL and return it.

    Raises ValueError if not found.
    """
    r = requests.get(lcsc_product_link(lcsc_id), headers=LCSC_SCRAPE_HEADERS)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    # Look for <meta name="og:image" content="..."> or property="og:image"
    for query in ({"name": "og:image"}, {"property": "og:image"}):
        if (meta_og_image := soup.find("meta", attrs=query)) and (content := meta_og_image.get("content")):  # type: ignore[arg-type]
            assert isinstance(content, str), f"Expected str, got {type(content)}"
            return content

    raise ValueError(f"No og:image found on LCSC page for {lcsc_id}")


def upload_lcsc_images(api: InvenTreeAPI):
    """
    Finds parts from LCSC that don't yet have images, and fills them from LCSC.
    Offers a [y/n/a/q] prompt: yes / no / yes to all / quit.
    """

    print("Gathering parts from server...")
    confirm_all = False

    # API calls are slow, frontload them.
    all_parts = Part.list(api)
    all_supplier_parts = SupplierPart.list(api)
    lcsc = unwrap_singleton(Company.list(api, name="LCSC"))

    # Skip parts that have an image.
    candidate_parts = [
        p for p in all_parts if p.thumbnail == "/static/img/blank_image.thumbnail.png" or not p.thumbnail
    ]

    for p in candidate_parts:
        log = logger.bind(link=part_url(api, p), name=p.name)

        lcsc_from_link = parse_url_for_lcsc_id(p.link)

        # Gather LCSC from single supplier
        sp_lcsc = [sp for sp in all_supplier_parts if sp.part == p.pk and sp.supplier == lcsc.pk]
        if len(sp_lcsc) != 1:
            log.info(f"Skip, {len(sp_lcsc)} LCSC SupplierParts.")
            continue
        lcsc_from_supplier = sp_lcsc[0].SKU

        # Decide if we have an LCSC ID
        if lcsc_from_link and lcsc_from_supplier:
            # If both are present, assert they match
            if lcsc_from_link != lcsc_from_supplier:
                raise ValueError(f"Conflicting LCSC IDs: {lcsc_from_link=} != {lcsc_from_supplier=}", log._context)
            # Both match => use either one
            lcsc_id = lcsc_from_link
        elif lcsc_from_link or lcsc_from_supplier:
            # Only one is present
            lcsc_id = lcsc_from_link or lcsc_from_supplier
        else:
            log.info("Skip, no LCSC source.")
            # Not an LCSC part or can't detect => skip
            continue

        # Show info
        print("\n----------------------------------------")
        print(f"Part:   {part_url(api, p)}")
        print(f"Name:   {p.name}")
        print(f"LCSC:   {lcsc_id}")
        # We'll attempt to fetch the LCSC image URL
        try:
            image_url = scrape_lcsc_image_url(lcsc_id)
        except Exception:
            log.exception("Failed to get image URL from LCSC")
            raise

        print(f"LCSC image: {image_url}")

        if not confirm_all:
            ans = choose("Upload image to InvenTree?", ["y", "n", "a", "q"])
            if ans == "q":
                print("Quitting.")
                return
            if ans == "a":
                confirm_all = True
            elif ans == "n":
                print("Skipping this part.")
                continue

        print("Downloading image...")
        try:
            r = requests.get(image_url, headers=LCSC_SCRAPE_HEADERS)
            r.raise_for_status()
        except Exception:
            log.exception("Error downloading part image")
            raise
        # part.uploadImage(...) expects a path, save to a temp file
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=True) as f:
            f.write(r.content)
            f.flush()
            p.uploadImage(f.path)

    print("\nDone uploading images for LCSC parts.")
