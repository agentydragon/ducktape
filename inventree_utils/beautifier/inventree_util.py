from inventree.api import InvenTreeAPI
from inventree.part import Part


def part_url(api: InvenTreeAPI, part: Part) -> str:
    base = api.base_url.rstrip("/")
    return f"{base}/part/{part.pk}/"
