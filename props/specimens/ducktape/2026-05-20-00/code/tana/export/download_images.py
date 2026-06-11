"""Download all media files referenced in a Tana JSON export."""

from __future__ import annotations

import argparse
import html
import json
import logging
import mimetypes
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse

from tana.domain.nodes import BaseNode
from tana.graph.graph import TanaGraph
from tana.graph.loader import load_workspace
from tana.query.nodes import get_image_url, get_mime_type

logger = logging.getLogger(__name__)


def _extension_from_mime(mime: str) -> str:
    """Convert MIME type to file extension (without dot)."""
    ext = mimetypes.guess_extension(mime)
    if ext:
        return ext.lstrip(".")
    # mimetypes maps image/jpeg to .jpeg, but .jpg is more conventional
    return mime.split("/", 1)[-1]


def _extension_from_url(url: str) -> str | None:
    """Try to extract extension from a Firebase Storage URL path."""
    parsed = urlparse(url)
    # Firebase URLs encode the path: /v0/b/.../o/notespace%2F...%2Fuploads%2Ftimestamp-filename.ext
    decoded_path = unquote(parsed.path)
    suffix = Path(decoded_path).suffix
    if suffix:
        return suffix.lstrip(".")
    return None


def _collect_media_nodes(store: TanaGraph) -> list[tuple[BaseNode, str]]:
    """Find all nodes with a media URL. Returns (node, url) pairs."""
    return [(node, html.unescape(url)) for node in store.values() if (url := get_image_url(node, store))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Path to Tana JSON export")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output directory for downloaded media")
    parser.add_argument("--dry-run", action="store_true", help="List media URLs without downloading")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    logger.info("Loading workspace from %s...", args.input)
    store = load_workspace(args.input)

    logger.info("Scanning for media nodes...")
    media_nodes = _collect_media_nodes(store)
    logger.info("Found %d media references", len(media_nodes))

    if not media_nodes:
        return

    if args.dry_run:
        for node, url in media_nodes:
            mime_label = get_mime_type(node, store) or "unknown"
            logger.info("  %s  %s  %s", node.id, mime_label, url)
        return

    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, str | int | None]] = []
    downloaded = 0
    failed = 0

    for i, (node, url) in enumerate(media_nodes, 1):
        mime = get_mime_type(node, store)
        ext = _extension_from_mime(mime) if mime else (_extension_from_url(url) or "bin")

        filename = f"{node.id}.{ext}"
        dest = output_dir / filename

        entry: dict[str, str | int | None] = {
            "node_id": node.id,
            "filename": filename,
            "url": url,
            "mime_type": mime,
            "owner_id": node.props.owner_id,
            "image_width": node.props.image_width,
            "image_height": node.props.image_height,
        }

        if dest.exists():
            logger.info("[%d/%d] %s (already exists)", i, len(media_nodes), filename)
            manifest.append(entry)
            downloaded += 1
            continue

        try:
            urllib.request.urlretrieve(url, dest)
            logger.info("[%d/%d] %s", i, len(media_nodes), filename)
            downloaded += 1
        except Exception:
            logger.exception("[%d/%d] FAILED %s", i, len(media_nodes), filename)
            failed += 1

        manifest.append(entry)

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    logger.info("Done: %d downloaded, %d failed. Manifest: %s", downloaded, failed, manifest_path)


if __name__ == "__main__":
    main()
