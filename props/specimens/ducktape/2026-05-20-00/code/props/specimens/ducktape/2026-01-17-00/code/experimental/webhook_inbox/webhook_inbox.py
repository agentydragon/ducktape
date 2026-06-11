"""
export WEBHOOK_INBOX_KEY='fgBWt1JKhqE6MbZAUntgZ7QBGJ0thPU1Su1qzU529l4='
uvicorn webhook_inbox:app --host 0.0.0.0 --port 8000
"""

import base64
import binascii
import contextlib
import json
import logging
import os
import pickle
import sqlite3
import sys
import time
import zlib
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from compact_json import Formatter
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import URL

WEBHOOK_INBOX_KEY: str = os.getenv("WEBHOOK_INBOX_KEY", "")  # 44-char url-safe b64, or unset → no crypto

MAX_PAYLOAD = int(os.getenv("MAX_PAYLOAD", "16384"))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "50"))
TZ = "America/Los_Angeles"
PAC = ZoneInfo(TZ)

templates = Jinja2Templates(directory="templates")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# Configure logging (avoid double config when uvicorn already set it up)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s - %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )

logger = logging.getLogger("webhook_inbox")
logger.setLevel(LOG_LEVEL)

# ─────────────────────────────────────────────────────────────────────────────
# Encoder
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Shared crypto helpers
# ─────────────────────────────────────────────────────────────────────────────


def encrypt_events(events, key: str) -> str:
    """Serialize *events* and return ciphertext string encrypted with *key*."""

    # 1. pickle → compress for smaller payloads
    packed = zlib.compress(pickle.dumps(events, protocol=5), level=9)

    # 2. Fernet encrypt (key must already be validated elsewhere)
    return Fernet(key).encrypt(packed).decode()


DECRYPT_CODE_SNIPPET = (
    "import zlib, pickle\n"
    "from cryptography.fernet import Fernet\n"
    "packed = Fernet(KEY).decrypt(CIPHERTEXT.encode())\n"
    "events = pickle.loads(zlib.decompress(packed))\n"
)


# ─────────────────────────────────────────────────────────────────────────────
# Encoder class (uses helpers above)
# ─────────────────────────────────────────────────────────────────────────────


class EncryptedEncoder:
    """Encode events, optionally encrypting them.

    Behaviour depends on three factors:

    1. If key is not set, return plain JSON.
    2. *Client-supplied key* - when a key is set, clients can also
       get plain JSON by supplying correct key in URL (``?key=…``/``/k/…```).
    """

    key: str | None

    @property
    def fernet(self):
        return Fernet(self.key) if self.key else None

    @staticmethod
    def _validate_key(key: str):
        try:
            key_bytes = base64.urlsafe_b64decode(key)
        except binascii.Error:
            raise RuntimeError("Key must be a url-safe base64 string.")
        if len(key_bytes) != 32:
            raise RuntimeError("Key has wrong length.")

    def __init__(self, key: str | None):
        # No key → encryption disabled.
        if not key:
            self.key = None
            return
        self._validate_key(key)
        self.key = key

    def plain_encode(self, events):
        # return json.dumps(events, separators=(',', ':')).encode()  # Compact JSON
        # return json.dumps(events, indent=2)  # pretty JSON
        return Formatter(indent_spaces=2, max_inline_length=70, max_inline_complexity=10).serialize(events)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def encode(self, events, *, provided_key: str | None = None):
        """Return a *dict* with encoded representation of *events*.

        Return type is a mapping for unpacking into Jinja context.

        Behaviour:

        * No key configured → ``{"plaintext": <pretty JSON>}``

        * Correct key supplied in the request (``?key=…``) → same - plaintext JSON.

        * Key not passed or incorrect →
          ``{"ciphertext_body": …, "ciphertext_len": …, "tz": …}``
          plus ``error`` entry if wrong key was supplied.
        """

        # ── Plain JSON path -------------------------------------------------
        # When no *server*-side key is configured **or** the caller supplied
        # the correct key, return events as pretty-printed JSON.

        # If the *server* is not configured with a key, encryption is entirely
        # disabled and we always serve plaintext JSON irrespective of any
        # client-supplied ``?key`` parameter.  This matches the behaviour
        # documented in the function's docstring and avoids runtime errors when
        # attempting to instantiate ``Fernet(None)``.

        if not self.key:
            return {"plaintext": self.plain_encode(events)}

        out = {}

        if provided_key is not None:
            if provided_key == self.key:
                return {"plaintext": self.plain_encode(events)}

            out["error"] = "Incorrect key in URL - data are still encrypted."

        # ── Encrypted path --------------------------------------------------
        ciphertext = encrypt_events(events, self.key)

        # 2. break into ≤50-char chunks and tag each line “# line i/N” for
        #    readability when embedded into documentation.
        width = 60
        chunks = [ciphertext[i : i + width] for i in range(0, len(ciphertext), width)]
        total = len(chunks)
        body = "\n".join(f"  {chunk!r}  # line {i + 1}/{total}" for i, chunk in enumerate(chunks))

        return out | {
            "ciphertext_body": "(\n" + body + "\n)",
            "ciphertext_len": len(ciphertext),
            "tz": TZ,
            "decrypt_code": DECRYPT_CODE_SNIPPET,
        }


# ── Encoder setup ----------------------------------------------------------
# A *single* encoder instance is sufficient - it decides at runtime whether to
# encrypt or not based on the presence of a configured key and the client's
# supplied `?key=` parameter.

ENCODER = EncryptedEncoder(WEBHOOK_INBOX_KEY)

# Global database connection, initialized by configure_db()
CONN: sqlite3.Connection


def configure_db(path: str | os.PathLike):
    """(Re-)initialise the global SQLite connection and schema.

    Tests use this function to point the application at an isolated temporary
    database **without** having to re-import the module. Production code can
    simply rely on the implicit initialisation that happens on import.
    """

    global CONN  # noqa: PLW0603

    # Close previous connection (if any) to avoid file locks under Windows
    # and to make sure commits hit the right file.
    if "CONN" in globals():
        with contextlib.suppress(Exception):
            # Ignore errors when connection already closed.
            CONN.close()

    # (Re-)create connection and ensure tables exist.
    CONN = sqlite3.connect(str(path), check_same_thread=False)
    CONN.execute(
        """CREATE TABLE IF NOT EXISTS events(
               id      INTEGER PRIMARY KEY,
               ts      INTEGER,
               payload TEXT)"""
    )
    CONN.execute(
        """CREATE TABLE IF NOT EXISTS access_log(
               id      INTEGER PRIMARY KEY,
               ts      INTEGER,
               path    TEXT,
               method  TEXT,
               query   TEXT,
               payload TEXT,
               headers TEXT,
               status  INTEGER)"""
    )
    return CONN


# Initial default connection on module import.
# Database is configurable at runtime via `configure_db` for tests.
configure_db(os.getenv("DB_PATH", "events.db"))

# ── FastAPI + access-logging middleware
app = FastAPI()


def _print_startup_banner() -> None:
    """Emit helpful links to stdout once at startup."""

    if any(mod.startswith("pytest") for mod in sys.modules):
        return

    index_url = "http://127.0.0.1:8000/"

    lines: list[str] = ["📬  Webhook Inbox ready", f"  UI → {index_url}"]
    # Expose plaintext link only when stdout is attached to a TTY.
    # In typical production stdout is captured by supervisor (systemd, docker,
    # kubernetes, …) or redirected into a log file and ``isatty()`` will be
    # False. Running locally in a terminal (e.g. via ``uvicorn webhook_inbox:app
    # --reload``) the call returns *True* so we show the convenience link.
    if WEBHOOK_INBOX_KEY and sys.stdout.isatty():
        lines.append("  Unencrypted UI →")
        lines.append(f"    {index_url}?key={WEBHOOK_INBOX_KEY}")
        lines.append(f"    {index_url}k/{quote(WEBHOOK_INBOX_KEY)}")
    lines.extend(["", "Send a test webhook:", f"""   curl -X POST {index_url} -d '{json.dumps({"hello": "world"})}'"""])

    print("\n".join(lines))


# Trigger banner emission.
_print_startup_banner()


@app.middleware("http")
async def log_all(req: Request, call_next):
    """Log every HTTP request.

    Two separate sinks are used:
    1. A *database* row that also stores (truncated) request bodies for later
       inspection in the web UI.
    2. A *stdout* line for operators.  **Bodies are *not* included** here to
       avoid leaking sensitive data into log aggregators.
    """

    ts = int(time.time())

    # Capture at most MAX_PAYLOAD bytes for the database, but *do not* emit
    # them to stdout logs.
    body = (await req.body())[:MAX_PAYLOAD].decode(errors="replace")

    # Forward the request downstream and capture the response.
    resp = await call_next(req)

    # Store in DB for UI.
    CONN.execute(
        "INSERT INTO access_log(ts,path,method,query,payload,headers,status) VALUES(?,?,?,?,?,?,?)",
        (ts, req.url.path, req.method, req.url.query, body, json.dumps(dict(req.headers.items())), resp.status_code),
    )
    CONN.commit()

    # Emit operator log (no body).
    logger.info(
        "handled_request",
        extra={
            "method": req.method,
            "path": req.url.path,
            "query": req.url.query,
            "status": resp.status_code,
            "ua": req.headers.get("user-agent", ""),
        },
    )

    # Add security headers to response we return upstream.
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")

    return resp


# ── POST /  → ingest event (no key in path)
@app.post("/")
async def ingest(req: Request):
    raw = await req.body()
    if len(raw) > MAX_PAYLOAD:
        raise HTTPException(413, "Payload too large")
    try:
        payload = raw.decode()
    except UnicodeDecodeError:
        raise HTTPException(400, "Payload must be valid UTF-8")
    CONN.execute("INSERT INTO events(ts,payload) VALUES(?,?)", (int(time.time()), payload))
    CONN.commit()
    return {"status": "ok"}


# ── Helper: render event list (shared by both “/” and “/k/{key}/” routes) ──


def _render_events_page(
    req: Request,
    *,
    key_value: str | None,
    key_style: str | None,  # "query", "path", or None
) -> RedirectResponse | object:
    """Common implementation for the event listing page.

    key_value: Key supplied by client or *None* if none was passed.
    key_style: How the key has been conveyed - ``"query"`` for ``?key=…``,
               ``"path"`` for ``/k/<key>/`` and *None* when no key is present.
    """

    # ------------------------------------------------------------------
    # 1. Ensure *before* and *count* query params are present
    # ------------------------------------------------------------------

    params: dict[str, str] = dict(req.query_params)

    redirect_needed = False

    if "before" not in params:
        params["before"] = str(int(time.time()))
        redirect_needed = True

    if "count" not in params:
        params["count"] = str(PAGE_SIZE)
        redirect_needed = True

    if redirect_needed:
        # Recreate original path (with key embedded if applicable) so sharing
        # the resulting URL preserves the user's chosen addressing scheme.
        base_path = req.url.path if key_style == "path" and key_value else "/"

        redirect_target = str(URL(base_path).include_query_params(**params))
        # Also propagate *query-style* key if that's how it was supplied.
        if key_style == "query" and key_value:
            redirect_target = str(URL(redirect_target).include_query_params(key=key_value))

        return RedirectResponse(url=redirect_target, status_code=302)

    # ------------------------------------------------------------------
    # 2. Parse and validate paging parameters
    # ------------------------------------------------------------------
    try:
        before_ts = int(params["before"])
    except ValueError:
        raise HTTPException(400, "Invalid 'before' parameter - must be integer timestamp")

    try:
        count = int(params["count"])
    except ValueError:
        raise HTTPException(400, "Invalid 'count' parameter - must be positive integer")

    if not (1 <= count <= PAGE_SIZE):
        raise HTTPException(400, f"'count' must be between 1 and {PAGE_SIZE}")

    rows = CONN.execute(
        "SELECT id,ts,payload FROM events WHERE ts < ? ORDER BY ts DESC LIMIT ?", (before_ts, count)
    ).fetchall()

    def _payload_entry(payload):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload

    events = [{"id": r[0], "ts": r[1], "payload": _payload_entry(r[2])} for r in rows]

    # ------------------------------------------------------------------
    # 3. Build navigation links (older / latest)
    # ------------------------------------------------------------------

    # Determine base path *including* the embedded key when key-style is
    # "path" so navigation retains the same addressing scheme the user chose.
    # Determine base path *including* the embedded key when key-style is
    # "path" so navigation retains the same addressing scheme the user chose.
    base_path = req.url.path if key_style == "path" and key_value else "/"

    older_link: str | None = None
    if rows:
        oldest_ts = rows[-1][1]
        if CONN.execute("SELECT 1 FROM events WHERE ts < ? LIMIT 1", (oldest_ts,)).fetchone():
            older_link = str(URL(base_path).include_query_params(before=oldest_ts, count=count))

    # Propagate *query-style* key only when the correct key was provided and the
    # user is indeed using the query scheme.
    latest_link: str = base_path
    if older_link and key_style == "query" and key_value == WEBHOOK_INBOX_KEY:
        if older_link:
            older_link = str(URL(older_link).include_query_params(key=key_value))
        latest_link = str(URL(latest_link).include_query_params(key=key_value))

    # ------------------------------------------------------------------
    # 4. Assemble template context
    # ------------------------------------------------------------------

    ctx = {
        "request": req,
        "events_count": len(events),
        "older_link": older_link,
        "latest_link": latest_link,
        **ENCODER.encode(events, provided_key=key_value),
    }

    if WEBHOOK_INBOX_KEY and key_value != WEBHOOK_INBOX_KEY:
        ctx["decrypt_link"] = str(URL(str(req.url)).include_query_params(key="KEY"))

    # Human-readable interval description ---------------------------------
    def fmt_ts(ts):
        return datetime.fromtimestamp(ts, PAC).isoformat(timespec="seconds")

    if events:
        start_iso = fmt_ts(events[-1]["ts"])
        if not older_link:
            start_iso = f"{start_iso} (beginning of history)"
        ctx["interval_str"] = f"[{start_iso}, {fmt_ts(before_ts)})"
    else:
        ctx["interval_str"] = f"(-∞, {fmt_ts(before_ts)})"

    return templates.TemplateResponse("events.html", ctx)


# ── GET /  → paged listing (no key in path) ───────────────────────────────


@app.get("/")
def list_events(req: Request):
    return _render_events_page(req, key_value=req.query_params.get("key"), key_style="query")


# ── GET /k/{key}/  → paged listing with key embedded in path  ─────────────


@app.get("/k/{key_value}/")
def list_events_key_in_path(req: Request, key_value: str):
    """Same as ``/`` but with *key* encoded as part of the URL path."""
    return _render_events_page(req, key_value=key_value, key_style="path")


# ── robots.txt  → disallow crawling
@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return FileResponse("robots.txt", media_type="text/plain")
