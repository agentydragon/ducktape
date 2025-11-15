import re

LCSC_PRODUCT_URL_RE = re.compile(r"https?://www\.lcsc\.com/product-detail/(?:[^/]*_)?(?P<id>C\d+)\.html(?:\?.*)?")


def parse_url_for_lcsc_id(url: str) -> str | None:
    """
    Given a possible LCSC product link, extract out the LCSC ID.
      e.g. https://www.lcsc.com/product-detail/XYZ_C12345.html -> 'C12345'
    Returns None if URL is not recognized.
    """
    match = LCSC_PRODUCT_URL_RE.fullmatch(url)
    return match.group("id") if match else None


def lcsc_product_link(lcsc_id: str) -> str:
    return f"https://www.lcsc.com/product-detail/{lcsc_id}.html"
