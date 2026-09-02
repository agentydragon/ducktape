"""Read-only, namespace-filtering Loki query proxy.

Loki runs with ``auth_enabled: false`` (namespace ``loki``): anyone who can
reach it can read, write, or delete *all* cluster logs. This proxy gives
agents log read access scoped per namespace:

- Only ``GET /loki/api/v1/query_range`` and ``GET /loki/api/v1/query`` are
  exposed. Every other path and method returns 404 — notably ``/series``,
  ``/labels``, ``/tail``, and the delete API.
- The ``query`` parameter must be a LogQL *log* query whose leading stream
  selector pins ``namespace`` with an exact ``=`` matcher (see
  ``parse_log_query_namespaces`` for the security argument).
- Each pinned namespace is authorized in one of two ways:

  - ``Authorization: Bearer <kubernetes token>``: the proxy issues a
    SelfSubjectAccessReview for ``get pods/log`` in that namespace **with the
    caller's own token**, so the decision is exactly the caller's Kubernetes
    RBAC (the same ``agent-readable-logs`` RoleBindings that gate
    ``kubectl logs``). The apiserver authenticates the token; the proxy holds
    no credential of its own. A token the apiserver rejects answers 401, an
    RBAC denial 403, and an apiserver that cannot be consulted 502 — never a
    fall-through to the allowlist below.
  - No ``Authorization`` header: the namespace must be in the static
    ``NAMESPACE_ALLOWLIST``. This path exists for callers that mount no
    Kubernetes token (the Haku sandbox pods) and is fenced by network policy.

- ``limit`` is capped at ``MAX_LIMIT`` (rejected with 400 above it) and
  defaults to ``DEFAULT_LIMIT``; only known-safe parameters are forwarded.
  The caller's ``Authorization`` header is never forwarded to Loki.
- An upstream that times out answers 504 and one that is unreachable answers
  502, rather than the bare 500 an unhandled ``httpx`` error would produce.
  Upstream responses that *arrive* pass through with their own status.

Who may reach the proxy at all is the CiliumNetworkPolicy in
cluster/k8s/agents/loki-read-proxy/ (ingress only from ``haku-sandbox`` pods,
egress only to Loki, the kube-apiserver, and DNS), together with Loki's own
ingress policy (cluster/k8s/monitoring/loki/cilium-network-policy.yaml),
which admits this proxy but not agent namespaces. Those manifests deploy this
code; they live under k8s/ because that is where Flux reads config from.

Configuration (env): ``NAMESPACE_ALLOWLIST`` (comma-separated, required),
``UPSTREAM_URL`` (default ``http://loki-gateway.loki.svc:80``),
``KUBERNETES_SERVICE_HOST``/``KUBERNETES_SERVICE_PORT`` (injected by the
kubelet) and ``KUBERNETES_CA_FILE`` (default: the in-cluster ServiceAccount
``ca.crt``; the deployment mounts ``kube-root-ca.crt`` there instead because
it mounts no ServiceAccount token).
"""

import logging
import os
import re
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 1000
MAX_LIMIT = 5000

# Upstream read budget. Named because the 504 body quotes it: a caller told
# "no response within 30s" can act on it, where a bare timeout cannot.
UPSTREAM_TIMEOUT_S = 30.0

# Budget for one SelfSubjectAccessReview round trip. Authorization must fail
# closed quickly rather than hold a query open behind an unresponsive apiserver.
KUBE_TIMEOUT_S = 5.0

SELF_SUBJECT_ACCESS_REVIEW_PATH = "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"

# Loki read-API parameters forwarded verbatim; anything else is dropped so the
# upstream only ever sees parameters this proxy understands. `time` is the
# instant-query (`/query`) evaluation timestamp.
_FORWARDED_PARAMS = ("start", "end", "since", "step", "direction", "time")

# Whitespace the LogQL lexer skips. Deliberately narrower than str.strip()'s
# default: a query led by any *other* codepoint must not reach the
# starts-with-`{` check as if that codepoint were blank.
_WHITESPACE = " \t\r\n"

_LABEL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

# Kubernetes Namespace names are RFC 1123 labels. A pinned value outside this
# grammar can name no namespace, so no RBAC decision could ever admit it —
# reject it structurally instead of asking the apiserver about it.
_NAMESPACE_NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")


class QueryValidationError(Exception):
    """Query failed validation and must not be forwarded to Loki."""


class MatcherOp(StrEnum):
    EQ = "="
    NEQ = "!="
    RE = "=~"
    NRE = "!~"


# Longest first, so `=~` is not lexed as `=` followed by junk.
_OPS = (MatcherOp.RE, MatcherOp.NRE, MatcherOp.NEQ, MatcherOp.EQ)


@dataclass(frozen=True)
class Matcher:
    label: str
    op: MatcherOp
    # Text between the quotes with escape sequences NOT decoded. See
    # parse_log_query_namespaces for why authorizing this raw form is sound.
    raw_value: str


def _parse_string(text: str, i: int) -> tuple[str, int]:
    """Parse the LogQL string literal starting at ``text[i]``.

    Returns (raw content between the quotes, index just past the closing
    quote). Supports the three PromQL/LogQL string forms: double- and
    single-quoted (backslash escapes a character, so ``\\"`` does not
    terminate) and backtick raw strings (no escapes at all).
    """
    quote = text[i]
    if quote == "`":
        end = text.find("`", i + 1)
        if end == -1:
            raise QueryValidationError("unterminated backtick string")
        return text[i + 1 : end], end + 1
    if quote not in ('"', "'"):
        raise QueryValidationError(f"expected quoted string at offset {i}")
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2  # skip the escaped character, whatever it is
            continue
        if text[j] == quote:
            return text[i + 1 : j], j + 1
        j += 1
    raise QueryValidationError("unterminated string")


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in _WHITESPACE:
        i += 1
    return i


def parse_leading_selector(query: str) -> tuple[list[Matcher], str]:
    """Parse the stream selector at the start of ``query`` (``query[0] == '{'``).

    Quote-and-escape-aware, so label values containing braces (``{pod="a}{b"}``)
    cannot trick the scanner into closing the selector early. Only the strict
    ``{label op "value", ...}`` grammar is accepted; anything else raises
    QueryValidationError (rejecting more than Loki would parse is always safe).

    Returns the parsed matchers and the remainder after the closing ``}``.
    """
    i = _skip_ws(query, 1)
    matchers: list[Matcher] = []
    while True:
        if i >= len(query):
            raise QueryValidationError("unterminated stream selector")
        if query[i] == "}":
            return matchers, query[i + 1 :]
        name = _LABEL_NAME_RE.match(query, i)
        if name is None:
            raise QueryValidationError(f"expected label name at offset {i}")
        i = _skip_ws(query, name.end())
        op = next((o for o in _OPS if query.startswith(o, i)), None)
        if op is None:
            raise QueryValidationError(f"expected matcher operator at offset {i}")
        i = _skip_ws(query, i + len(op))
        if i >= len(query):
            raise QueryValidationError("unterminated stream selector")
        raw_value, i = _parse_string(query, i)
        matchers.append(Matcher(label=name.group(), op=op, raw_value=raw_value))
        i = _skip_ws(query, i)
        if i >= len(query):
            raise QueryValidationError("unterminated stream selector")
        if query[i] == ",":
            i = _skip_ws(query, i + 1)
            continue
        if query[i] != "}":
            raise QueryValidationError(f"expected ',' or '}}' at offset {i}")


def _has_unquoted_brace(rest: str) -> bool:
    """True if ``rest`` contains a brace outside string literals."""
    i = 0
    while i < len(rest):
        c = rest[i]
        if c in ('"', "'", "`"):
            try:
                _, i = _parse_string(rest, i)
            except QueryValidationError:
                # Unterminated string: no unquoted brace can follow (everything
                # to EOF is inside the literal), and Loki rejects the query as
                # unparseable anyway.
                return False
            continue
        if c in "{}":
            return True
        i += 1
    return False


def parse_log_query_namespaces(query: str) -> frozenset[str]:
    """Return the namespaces ``query`` pins, or raise QueryValidationError.

    Only a log query whose leading stream selector pins ``namespace`` with
    exact ``=`` matchers passes; the returned values are the raw matcher
    values, which the caller then authorizes one by one.

    Security argument
    =================

    LogQL has two top-level expression kinds:

    - *Log* queries: exactly one leading stream selector ``{...}`` followed by
      an optional pipeline (line filters, parsers, label filters, formatters).
      Every pipeline stage operates within the streams matched by that one
      selector and can only narrow the result; the grammar has no construct
      that merges in a second selector (``{a} or {b}`` is a parse error).

    - *Metric* queries: range/vector aggregations wrapping a log query, e.g.
      ``sum(rate({...}[1m]))``, arithmetic between them, or literals. These
      always start with a function or aggregation identifier, a number, or
      ``(`` — never with ``{``.

    So requiring the first non-whitespace character to be ``{`` rejects every
    metric query — important because even a metric query that returns no log
    lines would act as a *count oracle* over forbidden namespaces (log volume,
    activity timing). Parsing the leading selector and requiring an exact
    ``namespace=`` matcher, authorized per caller, then pins the only stream
    set the query can touch: matchers within one selector are ANDed, so
    additional matchers (including additional ``namespace`` matchers) can only
    narrow it further.
    Whatever follows the selector either parses as a pipeline over those
    streams or fails to parse, in which case Loki answers 400 without touching
    any data.

    Belt and braces, we additionally reject any brace appearing *outside
    string literals* after the leading selector. Valid log-query pipelines
    only ever contain braces inside quoted strings (``| line_format
    "{{.msg}}"``, regex quantifiers like ``|~ "a{3}"``), so this costs no
    expressiveness — and it fails closed here rather than relying on Loki's
    parser, should LogQL ever grow a selector-combining construct.

    Authorizing the *raw* (undecoded) matcher value is sound because the raw
    value must itself be a valid namespace name (RFC 1123 label): such a value
    contains no escape sequences and decodes to itself in all three LogQL
    string syntaxes, while a value that would only decode *into* a real name
    via escapes (e.g. ``"fl\\165x-system"``) fails the grammar and is
    rejected.
    """
    stripped = query.strip(_WHITESPACE)
    if not stripped.startswith("{"):
        raise QueryValidationError(
            "only log queries are allowed: the query must start with a stream selector "
            "(metric queries would act as count oracles over forbidden namespaces)"
        )
    matchers, rest = parse_leading_selector(stripped)
    namespace_matchers = [m for m in matchers if m.label == "namespace"]
    if not namespace_matchers:
        raise QueryValidationError('the stream selector must pin namespace, e.g. {namespace="monitoring"}')
    for matcher in namespace_matchers:
        if matcher.op is not MatcherOp.EQ:
            raise QueryValidationError(f'namespace matchers must use exact "=" (got {matcher.op})')
        if _NAMESPACE_NAME_RE.fullmatch(matcher.raw_value) is None:
            raise QueryValidationError(f"{matcher.raw_value!r} is not a valid Kubernetes namespace name")
    if _has_unquoted_brace(rest):
        raise QueryValidationError("query must contain exactly one stream selector")
    return frozenset(m.raw_value for m in namespace_matchers)


@dataclass(frozen=True)
class Settings:
    upstream_url: str
    # Namespaces readable *without* a bearer token; a token-bearing request is
    # authorized by Kubernetes RBAC alone and never consults this set.
    namespace_allowlist: frozenset[str]
    kube_api_url: str
    # CA bundle that signs the apiserver's serving certificate; None trusts
    # the system store (an apiserver behind a public-CA certificate).
    kube_ca_file: Path | None

    @classmethod
    def from_env(cls) -> Self:
        allowlist = frozenset(ns.strip() for ns in os.environ["NAMESPACE_ALLOWLIST"].split(",") if ns.strip())
        if not allowlist:
            raise ValueError("NAMESPACE_ALLOWLIST must name at least one namespace")
        return cls(
            upstream_url=os.environ.get("UPSTREAM_URL", "http://loki-gateway.loki.svc:80"),
            namespace_allowlist=allowlist,
            kube_api_url=f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}",
            kube_ca_file=Path(
                os.environ.get("KUBERNETES_CA_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
            ),
        )


def bearer_token(authorization: str | None) -> str | None:
    """The token of a ``Bearer`` Authorization header; None when absent.

    Any other scheme, or an empty token, is 401: a caller who tried to
    authenticate must not silently fall back to the anonymous allowlist.
    """
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Authorization header must be 'Bearer <kubernetes token>'")
    return token


def self_subject_access_review(namespace: str) -> dict[str, Any]:
    """SelfSubjectAccessReview body asking whether the token's subject may read pod logs."""
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {
            "resourceAttributes": {
                "group": "",
                "namespace": namespace,
                "resource": "pods",
                "subresource": "log",
                "verb": "get",
            }
        },
    }


def _validated_limit(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    try:
        limit = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"limit is not an integer: {raw=}") from exc
    if not 1 <= limit <= MAX_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit out of range 1..{MAX_LIMIT}: {limit=}")
    return limit


def create_app(
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    kube_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the proxy app; tests inject the Loki and apiserver transports (httpx.MockTransport)."""
    http_client: httpx.AsyncClient | None = None
    kube_client: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal http_client, kube_client
        http_client = httpx.AsyncClient(base_url=settings.upstream_url, timeout=UPSTREAM_TIMEOUT_S, transport=transport)
        kube_client = httpx.AsyncClient(
            base_url=settings.kube_api_url,
            timeout=KUBE_TIMEOUT_S,
            transport=kube_transport,
            verify=ssl.create_default_context(cafile=str(settings.kube_ca_file)) if settings.kube_ca_file else True,
        )
        yield
        await http_client.aclose()
        await kube_client.aclose()
        http_client = kube_client = None

    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)

    async def _review_with_token(token: str, namespace: str) -> None:
        """Raise unless the apiserver says ``token``'s subject may get pods/log in ``namespace``."""
        assert kube_client is not None, "httpx client not initialized (lifespan not started)"
        try:
            resp = await kube_client.post(
                SELF_SUBJECT_ACCESS_REVIEW_PATH,
                json=self_subject_access_review(namespace),
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.RequestError as exc:
            # Includes timeouts: no decision means no access.
            logger.warning("SelfSubjectAccessReview for %s failed: %s", namespace, exc)
            raise HTTPException(status_code=502, detail=f"could not consult kube-apiserver: {exc}") from exc
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="kube-apiserver rejected the bearer token")
        if resp.status_code not in (200, 201):
            logger.warning("SelfSubjectAccessReview for %s answered HTTP %s", namespace, resp.status_code)
            raise HTTPException(status_code=502, detail=f"kube-apiserver answered HTTP {resp.status_code}")
        status = resp.json().get("status")
        if not isinstance(status, dict) or not isinstance(status.get("allowed"), bool):
            raise HTTPException(status_code=502, detail="kube-apiserver returned an incomplete SelfSubjectAccessReview")
        # allowed + evaluationError means an authorizer errored while another
        # allowed; like the Console's SAR client, treat that as no decision.
        if status.get("evaluationError"):
            logger.warning("SelfSubjectAccessReview for %s: %s", namespace, status["evaluationError"])
            raise HTTPException(status_code=502, detail="kube-apiserver reported an authorization evaluation error")
        if not status["allowed"] or status.get("denied"):
            reason = status.get("reason") or "RBAC denied"
            raise HTTPException(
                status_code=403, detail=f"not allowed to get pods/log in namespace {namespace!r}: {reason}"
            )

    async def _authorize(request: Request, namespaces: frozenset[str]) -> None:
        token = bearer_token(request.headers.get("authorization"))
        for namespace in sorted(namespaces):
            if token is not None:
                await _review_with_token(token, namespace)
            elif namespace not in settings.namespace_allowlist:
                raise HTTPException(
                    status_code=403,
                    detail=f"namespace {namespace!r} is not in the anonymous allowlist; "
                    "present a Kubernetes bearer token allowed to get pods/log there",
                )

    async def _proxy(request: Request, upstream_path: str) -> Response:
        match request.query_params.getlist("query"):
            case [query]:
                pass
            case []:
                raise HTTPException(status_code=400, detail="missing query parameter")
            case _:
                # Refuse ambiguity outright rather than picking one occurrence.
                raise HTTPException(status_code=400, detail="duplicate query parameter")
        try:
            namespaces = parse_log_query_namespaces(query)
        except QueryValidationError as exc:
            logger.info("rejected query %r: %s", query, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _authorize(request, namespaces)
        forwarded = {"query": query, "limit": str(_validated_limit(request.query_params.get("limit")))}
        forwarded |= {
            name: value for name in _FORWARDED_PARAMS if (value := request.query_params.get(name)) is not None
        }
        assert http_client is not None, "httpx client not initialized (lifespan not started)"
        try:
            resp = await http_client.get(upstream_path, params=forwarded)
        except httpx.TimeoutException as exc:
            # Must precede RequestError: TimeoutException is a subclass of it.
            logger.warning("upstream timed out after %ss for query %r", UPSTREAM_TIMEOUT_S, query)
            raise HTTPException(
                status_code=504,
                detail=(
                    f"Loki did not respond within {UPSTREAM_TIMEOUT_S}s. Narrow the time range, "
                    "lower limit, or add a line filter."
                ),
            ) from exc
        except httpx.RequestError as exc:
            logger.warning("upstream request failed for query %r: %s", query, exc)
            raise HTTPException(status_code=502, detail=f"could not reach Loki: {exc}") from exc
        return Response(content=resp.content, status_code=resp.status_code, media_type=resp.headers.get("content-type"))

    @app.get("/loki/api/v1/query_range")
    async def query_range(request: Request) -> Response:
        return await _proxy(request, "/loki/api/v1/query_range")

    @app.get("/loki/api/v1/query")
    async def query(request: Request) -> Response:
        return await _proxy(request, "/loki/api/v1/query")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Everything else — including non-GET methods on the allowed paths — is 404.
    # Registered last, so the routes above win; a full match here also prevents
    # Starlette's default 405 for method mismatches on the allowed paths.
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    async def not_found(path: str) -> Response:
        return JSONResponse({"detail": "not found"}, status_code=404)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    uvicorn.run(create_app(Settings.from_env()), host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
