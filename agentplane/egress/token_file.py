import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def read_token_file(path: Path) -> str | None:
    """A projected token, or None when its file is missing, unreadable, or empty. Callers re-read it
    on every use because kubelet rotates it in place."""
    try:
        token = path.read_text().strip()
    except OSError:
        logger.exception("token file %s unreadable", path)
        return None
    if not token:
        logger.error("token file %s is empty", path)
    return token or None
