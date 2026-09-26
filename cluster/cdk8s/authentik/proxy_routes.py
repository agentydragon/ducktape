"""HTTPRoutes for the apps behind Authentik's embedded proxy outpost, rendered into
`cluster/k8s/authentik/proxy-routes`: each host goes Gateway -> authentik-server (the outpost
authenticates) -> the provider's backend, as configured in the app's blueprint under
`cluster/k8s/authentik/app/blueprints/`.

haku-console and the Agentplane app own their OAuth and use direct HTTPRoutes in their own
namespaces.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
    HttpRouteSpecRulesFilters,
    HttpRouteSpecRulesFiltersResponseHeaderModifier,
    HttpRouteSpecRulesFiltersResponseHeaderModifierSet,
    HttpRouteSpecRulesFiltersType,
)

from cluster.cdk8s.gateway import cluster_gateway_parent_ref, https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "proxy-routes"
NAMESPACE = "authentik"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/authentik/proxy-routes"
_OUTPOST = "authentik-server"
_OUTPOST_PORT = 80

# Route name -> public hostname.
_ROUTES = {
    "hubble-ui": "hubble.allegedly.works",
    # Alloy OTLP: external clients authenticate with a Bearer token at the outpost.
    "alloy-otlp": "alloy-otlp.allegedly.works",
    "grocy-sf": "grocy-sf.allegedly.works",
    "grocy-vallejo": "grocy-vallejo.allegedly.works",
    # ActivityWatch's read-only proxy.
    "activitywatch": "activitywatch.allegedly.works",
    # The OpenClaw mitmproxy traffic viewer (admin-only).
    "agents-mitmproxy": "agents-mitmproxy.allegedly.works",
    # proxmox-proxy nginx -> atlas:8006.
    "proxmox": "atlas.allegedly.works",
    # The plaid-mcp web UI for Plaid Link.
    "plaid-mcp": "plaid-mcp.allegedly.works",
    "goldilocks-dashboard": "goldilocks.allegedly.works",
    # OpenWebRX+.
    "sdr": "sdr.allegedly.works",
    # The ADS-B ultrafeeder.
    "adsb": "adsb.allegedly.works",
}
# OpenClaw agents hold requests open for long model turns.
_LONG_REQUEST_ROUTES = {
    "public-coder-agent": "public-coder-agent.allegedly.works",
    "haku-openclaw-spike": "haku-openclaw-spike.allegedly.works",
}

# Inject the CSP on every haku-ui response, from OUTSIDE Haku's write scope.
# haku-ui's code is Haku-authored (adversarial under prompt injection); without this,
# its JS in the operator's browser could silently beacon personal data to third
# parties (fetch / <img> / sendBeacon) — subresource loads obey the DOCUMENT's own
# CSP, not the embedding console's (whose frame-src only fences navigations). This
# route is haku-ui's only public door and `set` overrides anything the backend
# sends, so the header is operator-enforced.
#
# The fence is a DESTINATION fence, not an execution fence (operator decision,
# 2026-08-01): every load/connect stays self / same-document — 'self' is
# Haku-authored by assumption, so constraining code CREATION bought nothing
# against the primary adversary. Execution is therefore relaxed route-wide:
# 'unsafe-eval' (JupyterLab's settings registry compiles JSON-schema validators
# via ajv `new Function`; without it Lab white-screens — observed live),
# worker-src blob: (Lab's workers), img-src blob: (widget/canvas outputs), and
# an explicit same-host wss: (older Safari doesn't fold wss into 'self').
# Accepted residuals of trading away no-eval, named so the decision is legible:
# (a) a string-to-eval gadget in the SPA or a dependency would let RENDERED
# external text execute directly, skipping the prompt-injection step; (b) such
# execution leaves no git commit (the PR path's audit property covers shipped
# code only); (c) no more loud CSP tripwire when a dependency grows an eval
# path. All bounded by the destination fence: whatever executes still can't
# beacon off-origin beyond the residuals below.
# Policy notes: 'unsafe-inline' style is for Mantine's injected <style> tags;
# everything the SPA legitimately loads is self-hosted (widen img-src by
# proxying, never by allowlisting third-party hosts); frame-ancestors permits
# only the trusted console (and self, for direct visits); the jupyter sidecar
# sets the same frame-ancestors list (jupyter/jupyter_server_config.py in
# haku-state) — this `set` overrides it at the edge, so keep them in sync.
# `webrtc 'block'` is CSP3 future-proofing only — no current browser enforces it
# (Chromium 141 logs "Unrecognized Content-Security-Policy directive 'webrtc'";
# Firefox/Safari never shipped it), so the WebRTC channel is an accepted residual.
# See haku/docs/security.md § Browser-side exfiltration + § Known gaps.
_HAKU_UI_CSP = " ".join(
    [
        "default-src 'self'; script-src 'self' 'unsafe-eval';",
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:;",
        "font-src 'self' data:; connect-src 'self' wss://haku-ui.allegedly.works;",
        "worker-src 'self' blob:; form-action 'self';",
        "frame-ancestors 'self' https://haku.allegedly.works;",
        "base-uri 'none'; object-src 'none'; webrtc 'block'",
    ]
)


def _proxy_route(chart: Chart, name: str, hostname: str, *, timeout: str | None = None) -> None:
    https_route(
        chart,
        name,
        metadata=metadata(name, NAMESPACE),
        hostname=hostname,
        backend=_OUTPOST,
        port=_OUTPOST_PORT,
        timeout=timeout,
        hsts=False,
        listener=None,
    )


def _haku_ui_route(chart: Chart) -> None:
    """The agent-authored Haku UI (Service haku-ui in haku-sandbox, the provider's internal_host),
    agentydragon-only. Operator-owned by construction: Kyverno forbids Haku creating routes in
    haku-sandbox, so this is haku-ui's only public path. Security model: haku/docs/security.md
    (enforcement inventory, "Authentik proxy route to haku-ui")."""
    HttpRoute(
        chart,
        "haku-ui",
        metadata=metadata("haku-ui", NAMESPACE),
        spec=HttpRouteSpec(
            parent_refs=[cluster_gateway_parent_ref()],
            hostnames=["haku-ui.allegedly.works"],
            rules=[
                HttpRouteSpecRules(
                    filters=[
                        HttpRouteSpecRulesFilters(
                            type=HttpRouteSpecRulesFiltersType.RESPONSE_HEADER_MODIFIER,
                            response_header_modifier=HttpRouteSpecRulesFiltersResponseHeaderModifier(
                                set=[
                                    HttpRouteSpecRulesFiltersResponseHeaderModifierSet(
                                        name="Content-Security-Policy", value=_HAKU_UI_CSP
                                    )
                                ]
                            ),
                        )
                    ],
                    backend_refs=[HttpRouteSpecRulesBackendRefs(name=_OUTPOST, port=_OUTPOST_PORT)],
                )
            ],
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for name, hostname in _ROUTES.items():
        _proxy_route(chart, name, hostname)
    for name, hostname in _LONG_REQUEST_ROUTES.items():
        _proxy_route(chart, name, hostname, timeout="600s")
    _haku_ui_route(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
