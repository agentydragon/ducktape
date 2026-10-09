"""The public-coder-agent app: its openclaw.json5 (see model_catalog/catalog.py for the model-name
scheme and the Codex/Gemini rosters this pulls from), the OpenClaw Deployment and everything
around it.

The image tag is the placeholder "unset"; the hand-written
cluster/k8s/agents/public-coder-agent/app/image-pins/kustomization.yaml overrides it at
`kustomize build` time via Flux's image-automation marker. The `ssh devbox` ConfigMap comes
from the kustomization.yaml's configMapGenerator.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from agentplane.egress.resources import placeholder_of
from cluster.cdk8s import agent_access_profiles as access, external_creds, namespaces
from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.config_format import json5_config, yaml_config
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.forgejo_registry import chart as forgejo_images
from cluster.cdk8s.generation import config_map_chart, write_charts
from cluster.cdk8s.haku import console, kube_api_proxy
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.openclaw_gateway import disabled_commands, session_memory_hook, trusted_proxy_gateway
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data
from cluster.cdk8s.public_coder import (
    egress as public_coder_egress,
    proxy as public_coder_proxy,
    sshpiper as public_coder_sshpiper,
)
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from model_catalog.catalog import (
    ANTIGRAVITY_FLASH_3,
    ANTIGRAVITY_FLASH_36,
    ANTIGRAVITY_FLASH_37,
    ANTIGRAVITY_FLASH_38,
    ANTIGRAVITY_FLASH_LITE_31,
    ANTIGRAVITY_GPT_OSS_120B_MEDIUM,
    ANTIGRAVITY_OPUS,
    ANTIGRAVITY_PRO,
    ANTIGRAVITY_PRO_LOW,
    ANTIGRAVITY_SONNET,
    GEMINI_ROUTES,
    GPT6_ASTRA_RESPONSES,
    GPT6_LUNA_RESPONSES,
    GPT6_SOL_RESPONSES,
    OLLAMA_EMBEDDING_ROUTE,
    Provider,
    Route,
)

_CONFIG_MAP_NAME = "config"
_NAME = "public-coder-agent"
NAMESPACE = public_coder_egress.NAMESPACE
LABELS = public_coder_egress.LABELS
# The gateway the Authentik outpost proxies to.
_SERVICE = ServiceRef(
    name=_NAME, port=Port(name="gateway", number=18789), pods=Pods(namespace=NAMESPACE, labels=tuple(LABELS.items()))
)
_NAMESPACE_ANNOTATIONS = {
    "description": (
        "Second OpenClaw agent, egress-confined to a CONNECT proxy and reachable only through the Authentik proxy "
        "outpost. Opens pull requests against public repositories as agentydragon-agent."
    )
}
_IMAGE = "git.allegedly.works/ducktape-ci/public-coder-agent:unset"
_HOME = "/home/openclaw"
_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
_STATE_CLAIM_NAME = "public-coder-agent-state-v2"
_DIAGNOSTICS_CLAIM_NAME = "public-coder-agent-diagnostics"
_GATEWAY_PASSWORD = SecretRef(namespace=NAMESPACE, name="public-coder-agent-gateway-password")
# Also the Matrix channel's `proxy` in config(): the same proxy performs Matrix login-password
# substitution.
_EGRESS_PROXY = public_coder_egress.PROXY_URL
_NO_PROXY = "127.0.0.1,localhost,litellm.litellm.svc,litellm.litellm.svc.cluster.local"
_KUBECONFIG_CONFIG_MAP_NAME = "kubeconfig"
# Rendered by the kustomization.yaml's configMapGenerator.
_SSH_CONFIG_MAP_NAME = "public-coder-agent-ssh"
_RBAC_GROUP = "rbac.authorization.k8s.io"
_READ = ["get", "list", "watch"]


def _model_entry(route: Route, *, context_budget: int, output_budget: int) -> dict:
    model = route.model
    if model.reasoning is None:
        raise ValueError(f"missing OpenClaw metadata for {route.id}")
    account_name = {
        Provider.CHATGPT: "Codex subscription",
        Provider.GOOGLE: "Google AI",
        Provider.ANTIGRAVITY: "Google Antigravity",
    }[route.upstream.provider]
    return {
        "contextWindow": context_budget,
        "id": route.id,
        "input": ["text", "image"],
        "maxTokens": output_budget,
        "name": f"{route.display_name} ({account_name} via LiteLLM)",
        "reasoning": model.reasoning,
    }


def config() -> dict:
    return {
        # This file is the GitOps source of truth. The app init container copies it
        # into the state PVC; OPENCLAW_CONFIG_PATH points OpenClaw back at that copy.
        "agents": {
            "ownership": "explicit",
            "defaults": {
                "userTimezone": "America/Los_Angeles",
                # The working path is Codex subscription -> CLIProxyAPI -> LiteLLM's
                # native Responses endpoint. Keep this on the measured 5.6 roster.
                "model": {"primary": f"litellm/{GPT6_LUNA_RESPONSES.id}"},
                "sandbox": {"mode": "off"},
                "maxConcurrent": 16,
                "subagents": {"maxConcurrent": 16, "maxChildrenPerAgent": 16},
                "skills": [],
                "verboseDefault": "full",
            },
            "entries": {
                "coder": {"name": "Coder"},
                "haku_console_tpm": {
                    "name": "Haku Console TPM",
                    "model": {"primary": f"litellm/{GPT6_ASTRA_RESPONSES.id}"},
                },
            },
        },
        # Route the Matrix account to the public coder agent explicitly; multi-agent
        # configurations do not infer a channel owner.
        "bindings": [{"agentId": "coder", "match": {"channel": "matrix"}}],
        # Memory uses LiteLLM's OpenAI-compatible embeddings route; keep it out
        # of the public egress proxy because LiteLLM is an in-cluster Service.
        "memory": {
            "search": {
                "enabled": True,
                "sources": ["memory"],
                "provider": "openai-compatible",
                # This is a new embedding identity; changing it deliberately requires a
                # full rebuild of the durable index after the rollout.
                "model": OLLAMA_EMBEDDING_ROUTE.id,
                "remote": {
                    "baseUrl": "http://litellm.litellm.svc.cluster.local:4000/v1",
                    "apiKey": "${OPENCLAW_LITELLM_API_KEY}",
                },
            }
        },
        "commands": disabled_commands(),
        "channels": {
            "matrix": {
                # MATRIX_PASSWORD is intentionally absent: OpenClaw reads the stable
                # placeholder from the environment, while Agentplane egress swaps the real
                # password only in Matrix's login body. The resulting access token is
                # cached in the state PVC.
                "enabled": True,
                "homeserver": "https://matrix.allegedly.works",
                # The Matrix plugin's guarded fetch reads this field explicitly; generic
                # HTTP(S)_PROXY is not enough.
                "proxy": _EGRESS_PROXY,
                "userId": "@public-coder-agent:allegedly.works",
                "dm": {
                    "policy": "allowlist",
                    "allowFrom": ["@agentydragon:allegedly.works"],
                    # Keep each DM room as an independent conversation.
                    "sessionScope": "per-room",
                },
                # Group rooms are intentionally not accepted.
                "groupPolicy": "disabled",
                # Matrix DMs arrive as invites, so classify them only after joining.
                "autoJoin": "always",
                # Preserve Matrix's threaded reply presentation.
                "threadReplies": "always",
            }
        },
        "cron": {"enabled": True, "triggers": {"enabled": True}},
        # Local backend and subagent calls have no Authentik headers, so they use
        # the generated gateway password supplied by the Deployment. Keep it out
        # of this file.
        "gateway": trusted_proxy_gateway(
            allowed_origin="https://public-coder-agent.allegedly.works",
            device_approve_scopes=[
                "operator.read",
                "operator.write",
                "operator.approvals",
                "operator.questions",
                "operator.admin",
            ],
        ),
        "hooks": session_memory_hook(),
        "models": {
            "providers": {
                # One provider intentionally covers both working model families. Codex
                # uses openai-responses and chatgpt/oai-responses/* through CLIProxyAPI;
                # Gemini uses the same LiteLLM Responses surface. No Anthropic provider.
                "litellm": {
                    "agentRuntime": {"id": "openclaw"},
                    "api": "openai-responses",
                    "apiKey": "${OPENCLAW_LITELLM_API_KEY}",
                    "baseUrl": "http://litellm.litellm.svc.cluster.local:4000/v1",
                    # Preserve the paused client's existing selection, order and budgets.
                    # These are OpenClaw settings, not provider capacity claims;
                    # adding provider metadata must not opt another route in.
                    "models": [
                        _model_entry(GPT6_ASTRA_RESPONSES, context_budget=872_000, output_budget=128_000),
                        *(
                            _model_entry(route, context_budget=372_000, output_budget=128_000)
                            for route in (GPT6_LUNA_RESPONSES, GPT6_SOL_RESPONSES)
                        ),
                        *(
                            _model_entry(route, context_budget=1_048_576, output_budget=65_536)
                            for route in GEMINI_ROUTES
                        ),
                        *(
                            _model_entry(route, context_budget=200_000, output_budget=64_000)
                            for route in (ANTIGRAVITY_OPUS, ANTIGRAVITY_SONNET)
                        ),
                        *(
                            _model_entry(route, context_budget=1_048_576, output_budget=65_536)
                            for route in (
                                ANTIGRAVITY_FLASH_36,
                                ANTIGRAVITY_FLASH_37,
                                ANTIGRAVITY_FLASH_38,
                                ANTIGRAVITY_FLASH_3,
                            )
                        ),
                        *(
                            _model_entry(route, context_budget=1_048_576, output_budget=65_535)
                            for route in (ANTIGRAVITY_PRO, ANTIGRAVITY_PRO_LOW)
                        ),
                        _model_entry(ANTIGRAVITY_GPT_OSS_120B_MEDIUM, context_budget=114_000, output_budget=32_768),
                        _model_entry(ANTIGRAVITY_FLASH_LITE_31, context_budget=1_048_576, output_budget=65_535),
                    ],
                    "request": {"allowPrivateNetwork": True},
                }
            }
        },
        "plugins": {
            "entries": {
                # Matrix is bundled into the image as a trusted plugin; loading a copy
                # through plugins.load.paths breaks its state-storage trust requirement.
                "matrix": {"enabled": True},
                "brave": {
                    "enabled": True,
                    "config": {
                        "webSearch": {
                            # Brave is bundled and image-pinned; the pod sees only a
                            # placeholder, which Agentplane egress replaces at Brave's API endpoint.
                            "apiKey": {"source": "env", "id": "BRAVE_API_KEY"},
                            "mode": "web",
                        }
                    },
                },
                # Disabled on this headless agent: companion-device plugins need a paired
                # phone/desktop, and Ollama is not used. No phone-control entry: this
                # OpenClaw build doesn't register that plugin, so configuring it is a
                # stale no-op the gateway flags on every startup.
                "canvas": {"enabled": False},
                "device-pair": {"enabled": False},
                "file-transfer": {"enabled": False},
                "talk-voice": {"enabled": False},
                "ollama": {"enabled": False},
            }
        },
        "tools": {
            # Cross-agent sessions_send/sessions_spawn targets are invisible under
            # the default "tree" scope. OpenClaw 2026.8.1 configures visibility only
            # at this global scope; agentToAgent below remains the exact agent gate.
            "sessions": {"visibility": "all"},
            # Off by default upstream: lets "haku_console_tpm" hand tasks to "coder"
            # via sessions_send/sessions_spawn. Both ids must be listed for either
            # direction of that handoff to be admitted.
            "agentToAgent": {"enabled": True, "allow": ["haku_console_tpm", "coder"]},
            "web": {
                "search": {
                    "enabled": True,
                    "provider": "brave",
                    "maxResults": 5,
                    "timeoutSeconds": 30,
                    "cacheTtlMinutes": 15,
                }
            },
        },
    }


def chart(app: App) -> Chart:
    return config_map_chart(
        app,
        chart_name=_CONFIG_MAP_NAME,
        configmap_name=_CONFIG_MAP_NAME,
        namespace=NAMESPACE,
        data={"openclaw.json5": json5_config(config())},
    )


def kubeconfig_chart(app: App) -> Chart:
    """kubectl's config: haku-kube-api-proxy's public route, reached through the egress proxy,
    with the bearer placeholder the proxy swaps."""
    chart = Chart(app, _KUBECONFIG_CONFIG_MAP_NAME, disable_resource_name_hashes=True)
    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "in-cluster",
                "cluster": {
                    "server": f"https://{kube_api_proxy.HOSTNAME}",
                    "proxy-url": _EGRESS_PROXY,
                    "certificate-authority": _CA_BUNDLE,
                },
            }
        ],
        "contexts": [
            {"name": "in-cluster", "context": {"cluster": "in-cluster", "namespace": NAMESPACE, "user": "haku-agent"}}
        ],
        "current-context": "in-cluster",
        # Agentplane egress substitutes the original Haku Agent bearer only for the dedicated Haku
        # Kubernetes proxy hostname. No Kubernetes credential enters this container.
        "users": [{"name": "haku-agent", "user": {"token": placeholder_of(public_coder_egress.HAKU_CREDENTIAL)}}],
    }
    k8s.KubeConfigMap(
        chart,
        "config",
        metadata=k8s.ObjectMeta(name=_KUBECONFIG_CONFIG_MAP_NAME, namespace=NAMESPACE),
        data={"config": yaml_config(kubeconfig)},
    )
    return chart


def _env(name: str, value: str) -> k8s.EnvVar:
    return k8s.EnvVar(name=name, value=value)


# Seed the GitOps config into the state PVC before startup. Remove the gateway's own config
# backups so the ConfigMap remains authoritative; see
# docs/personal_agents/findings/harness_behaviour.md F19.
_SEED_CONFIG_SCRIPT = textwrap.dedent(
    """\
    cp /cfg/openclaw.json5 /state/openclaw.json5
    rm -f /state/openclaw.json5.last-good /state/openclaw.json5.bak*
    """
)


def _openclaw_container() -> k8s.Container:
    return k8s.Container(
        name="openclaw",
        image=_IMAGE,
        env=[
            # The image does not set HOME to the state dir; without it the gateway looks under
            # the wrong home directory and exits "Missing config".
            _env("HOME", _HOME),
            _env("OPENCLAW_CONFIG_PATH", f"{_HOME}/.openclaw/openclaw.json5"),
            _env("NPM_CONFIG_PREFIX", f"{_HOME}/.local"),
            _env("NPM_CONFIG_CACHE", f"{_HOME}/.cache/npm"),
            # LiteLLM's Gemini embedding route is request-quota limited. Keep a single request
            # stream and wait long enough for its observed Retry-After windows instead of
            # compounding throttled requests.
            _env("OPENCLAW_MEMORY_INDEX_CONCURRENCY", "1"),
            _env("OPENCLAW_MEMORY_RETRY_BASE_DELAY_MS", "30000"),
            _env("OPENCLAW_MEMORY_RETRY_MAX_DELAY_MS", "120000"),
            # Full SQLite integrity checks cross-check every index against its table in
            # O(N log N) time. On this agent's rotational state disk, the already-current
            # database took over 20 minutes and outlived OpenClaw's 60-second default startup
            # migration lease. Even quick_check exceeded that lease on the live 871 MB database,
            # so skip startup integrity pragmas; schema and canonical-index checks still run, and
            # full integrity checks remain available offline.
            _env("OPENCLAW_AGENT_DB_STARTUP_INTEGRITY_CHECK", "none"),
            # Startup migrations hold a synchronous SQLite file-exclusion scope on the rotational
            # state disk. Keep the maintenance lease alive for that bounded recovery window
            # instead of letting the 60-second default expire.
            _env("OPENCLAW_AGENT_DB_MAINTENANCE_LEASE_MS", "86400000"),
            # Turn off the gateway's background database integrity verifier. Not an upstream
            # knob -- the release-specific patch under openclaw/patches adds it, because
            # upstream guards the verifier only behind a vitest-only test flag and hardcodes
            # its schedule.
            #
            # It forks a worker that copies every registered agent database out of this volume
            # into the container filesystem and scans the copy: 2.25 GiB read and 1.59 GiB
            # written per pass against a ~2.1 GB database set, on the rotational disk this agent
            # already saturates. It runs five minutes after start and then daily -- but this
            # gateway aborts on the V8 heap limit every ~2h50m, so the daily pass never arrives
            # and the five-minute one restarts from zero every boot. It has not been observed to
            # complete.
            #
            # Nothing else quarantines a corrupted state or agent database, so this trades a
            # check that is not finishing for the I/O it costs.
            #
            # CLEANUP(added 2026-09-11): restore "on" once the gateway stops aborting on the heap
            # limit, so the daily pass can actually run.
            _env("OPENCLAW_DATABASE_VERIFY", "off"),
            # Keep provider, compaction, and agent-loop debug records on stdout so cluster
            # logging captures them in Loki. OpenClaw's sensitive-value redaction remains enabled
            # at its default.
            _env("OPENCLAW_LOG_LEVEL", "debug"),
            # A placeholder, not a credential. The real PAT lives in the egress proxy, which swaps
            # this value into `Authorization` on the way out to GitHub -- in `Bearer <value>` and
            # inside base64 `Basic user:<value>` alike, so both the REST API and the git transport
            # work. The agent cannot read the token from its environment or from /proc, which is
            # what F7 previously left exposed.
            #
            # The agent has to be told the contract, because the proxy only acts on requests
            # carrying the placeholder: authenticate to GitHub with $GH_PAT as if it were a real
            # token. A request without it is not a failure, just an unauthenticated request.
            #
            # Also expose the same inert placeholder under GitHub's standard `GITHUB_TOKEN` name.
            # OpenClaw's generic exec environment filter treats it as a credential, although its
            # current local-Gateway path can restore ambient values for native GitHub tooling.
            # The proxy still sees only the placeholder and replaces it in scoped outbound
            # Authorization headers. See F7, F10, F16.
            _env("GITHUB_TOKEN", placeholder_of(public_coder_egress.GITHUB_CREDENTIAL)),
            _env("GH_PAT", placeholder_of(public_coder_egress.GITHUB_CREDENTIAL)),
            # Non-secret Haku Kubernetes proxy bearer placeholder. The real static-Agent
            # credential exists in Haku Console and Agentplane egress replaces this value only
            # in Authorization headers sent to haku-kubeapi.allegedly.works.
            _env("HAKU_CONSOLE_TOKEN", placeholder_of(public_coder_egress.HAKU_CREDENTIAL)),
            # Native ClickHouse reader credentials for normalized and raw AIQuota history. This
            # is deliberately a non-secret placeholder: the Agentplane egress gateway swaps it only
            # inside Authorization for the private ClickHouse ClusterIP host.
            _env("CLICKHOUSE_PUBLIC_CODER_USER", client.PUBLIC_CODER_USER),
            _env("CLICKHOUSE_PUBLIC_CODER_PASSWORD", placeholder_of(public_coder_egress.CLICKHOUSE_CREDENTIAL)),
            # This placeholder grants access only when Agentplane egress substitutes it for
            # aiquota.allegedly.works' two read-only API paths. The actual shared bearer is
            # mounted only into the proxy container.
            _env("AIQUOTA_API_BEARER_TOKEN", placeholder_of(public_coder_egress.AIQUOTA_CREDENTIAL)),
            # This is likewise an inert placeholder. The real Brave Search API key is mounted only
            # in the egress proxy and substituted solely in X-Subscription-Token requests to
            # api.search.brave.com.
            _env("BRAVE_API_KEY", placeholder_of(public_coder_egress.BRAVE_CREDENTIAL)),
            SecretRef(namespace=NAMESPACE, name="litellm-key-public-coder-agent")
            .key("api-key")
            .env_var("OPENCLAW_LITELLM_API_KEY"),
            # Authentik authenticates proxied browser traffic. OpenClaw's subagent completion
            # path calls the local gateway directly and therefore uses the documented
            # trusted-proxy local-password fallback instead of proxy identity headers.
            _GATEWAY_PASSWORD.key("password").env_var("OPENCLAW_GATEWAY_PASSWORD"),
            # OpenClaw's password login puts this value in the Matrix JSON body. It is a proxy
            # placeholder: Agentplane egress replaces it with the real controller-owned password only
            # on the Matrix login endpoint.
            _env("MATRIX_PASSWORD", placeholder_of(public_coder_egress.MATRIX_CREDENTIAL)),
            # Node does not honour proxy environment variables by default.
            _env("NODE_USE_ENV_PROXY", "1"),
            _env("HTTP_PROXY", _EGRESS_PROXY),
            _env("HTTPS_PROXY", _EGRESS_PROXY),
            # curl deliberately ignores uppercase HTTP_PROXY for plain HTTP (e.g. ClickHouse).
            _env("http_proxy", _EGRESS_PROXY),
            _env("https_proxy", _EGRESS_PROXY),
            # LiteLLM is in-cluster and must not go through the proxy. Do not bypass every
            # Service DNS name or the cluster Service CIDR: ClickHouse is intentionally sent
            # through Agentplane so its password placeholder cannot reach the ClusterIP service
            # unchanged.
            _env("NO_PROXY", _NO_PROXY),
            _env("no_proxy", _NO_PROXY),
            # The proxy terminates TLS, so its root must be trusted. The proxy's trust Bundle is
            # mounted over the system trust store below, which covers every OpenSSL and GnuTLS
            # client at once -- so no per-tool variables are needed.
            #
            # That is not tidying. `GIT_SSL_CAINFO` is one of the names OpenClaw strips from the
            # exec tool's environment, and git links GnuTLS, which reads neither `SSL_CERT_FILE`
            # (an OpenSSL variable) nor `CURL_CA_BUNDLE` (read by the curl binary, not by
            # libcurl) -- so git through this proxy could not verify at all. F17.
            #
            # Node is the exception: it carries its own roots and ignores the system store. Point
            # it at the MOUNTED path -- Node ignores a nonexistent file silently and then fails
            # with SELF_SIGNED_CERT_IN_CHAIN, which reads like a trust problem rather than a
            # typo. F18.
            _env("NODE_EXTRA_CA_CERTS", _CA_BUNDLE),
            # Python is the other runtime that ignores the mounted system bundle: pip trusts only
            # its vendored certifi, and hermetic interpreters carry their own OpenSSL whose
            # compiled-in CA path is not Debian's. Without these, pip fetches fail TLS against the
            # egress proxy even though pypi.org is allowlisted. Mirrors the same vars in
            # haku-openclaw-spike.
            _env("SSL_CERT_FILE", _CA_BUNDLE),
            _env("REQUESTS_CA_BUNDLE", _CA_BUNDLE),
            _env("PIP_CERT", _CA_BUNDLE),
        ],
        ports=[_SERVICE.port.k8s_container_port()],
        # Without this the Deployment reports 1/1 Running whenever a process exists, which hid
        # two different outages during the 2026.8.1 recovery: a gateway crash-looping every ~4
        # minutes, and one that started but never bound its port. /healthz answers 200
        # unauthenticated on a serving gateway (/readyz and / are auth-gated at 403), so it
        # checks that the HTTP server is really handling requests, not just that something holds
        # the port.
        #
        # Readiness only, deliberately no liveness or startup probe: startup is IO-bound on the
        # state PVC and legitimately takes many minutes -- a gateway on the OVH HDD was measured
        # reading ~940 KB/s, hundreds of MB in, still progressing. A restarting probe would kill
        # that and never let it finish. A failing readiness probe restarts nothing; it just makes
        # "not serving yet" visible, which is the whole point.
        readiness_probe=k8s.Probe(
            http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_string(_SERVICE.port.name)),
            initial_delay_seconds=10,
            period_seconds=10,
            timeout_seconds=5,
            failure_threshold=3,
        ),
        volume_mounts=[
            k8s.VolumeMount(name="data", mount_path=f"{_HOME}/.openclaw"),
            # Kubeconfig with a non-secret placeholder. Agentplane egress substitutes the Agent's Haku
            # bearer only for haku-kubeapi.allegedly.works; no Kubernetes credential is mounted.
            k8s.VolumeMount(name="kubeconfig", mount_path=f"{_HOME}/.kube", read_only=True),
            # Over the system trust store, not alongside it. The Bundle is built with
            # useDefaultCAs, so it already contains the public roots plus the cluster root plus
            # this proxy's interception root -- replacing the distro file loses nothing.
            k8s.VolumeMount(name="trust", mount_path=_CA_BUNDLE, sub_path="ca-certificates.crt", read_only=True),
            # `ssh devbox` -- config, host-key pin, and the Agent's own downstream key, projected
            # into one directory because ~/.ssh has to be a single path. See ./ssh_config.
            k8s.VolumeMount(name="ssh", mount_path=f"{_HOME}/.ssh", read_only=True),
            k8s.VolumeMount(name="tmp", mount_path="/tmp"),
            # Heap snapshots and Node reports. Separate from /tmp so a capture outlives the Pod;
            # see _claims. The path is half of a contract with
            # --diagnostic-dir/--report-directory in openclaw/default.nix -- moving it there
            # without moving it here leaves Node writing diagnostics into the container layer,
            # where the next roll discards them.
            k8s.VolumeMount(name="diagnostics", mount_path="/diag"),
        ],
        resources=k8s.ResourceRequirements(
            requests={"cpu": k8s.Quantity.from_string("250m"), "memory": k8s.Quantity.from_string("768Mi")},
            limits={
                "cpu": k8s.Quantity.from_string("2"),
                # openclaw/default.nix pins --max-old-space-size at exactly what this limit
                # derives today, so the two move together. The gap is not slack: it carries
                # native allocations, and it has to absorb the transient growth when V8 raises its
                # own limit to serialize a near-heap-limit snapshot. Raising this silently raises
                # nothing -- the heap is pinned -- but lowering it below that headroom turns a
                # clean heap-limit abort into an OOMKill.
                "memory": k8s.Quantity.from_string("4Gi"),
            },
        ),
    )


def _deployment(scope: Construct) -> None:
    """OpenClaw as a plain Deployment rather than an OpenClawInstance.

    Deviation from the retired operator-managed gateway, and the reason for it: the operator's
    generated NetworkPolicy always contains an egress rule for 443/TCP with no destination
    selector, and spec.security.networkPolicy offers only additive fields (additionalEgress,
    allowedEgressCIDRs) with no way to disable it. Kubernetes NetworkPolicies are unions of
    allows, so that rule cannot be subtracted -- an operator-managed instance cannot be
    egress-confined. Owning the Deployment means owning its egress NetworkPolicy outright.

    The cost is losing the operator's autoUpdate and CRD ergonomics. Everything else (image,
    config, state layout) is identical to the operator's shape.
    """
    k8s.KubeServiceAccount(
        scope,
        "openclaw-service-account",
        metadata=k8s.ObjectMeta(
            name=public_coder_egress.SERVICE_ACCOUNT,
            namespace=NAMESPACE,
            labels={"agentplane.allegedly.works/use-action-service": "true"},
        ),
        automount_service_account_token=False,
    )
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=NAMESPACE, labels=_SERVICE.pods.selector),
        spec=k8s.DeploymentSpec(
            # Keep the replica count GitOps-owned; the worker-local state claim is selected by the
            # affinity and PVC declarations below.
            replicas=0,  # Public Coder is paused; keep its storage and configuration for resuming.
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_SERVICE.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_SERVICE.pods.selector),
                spec=k8s.PodSpec(
                    service_account_name=public_coder_egress.SERVICE_ACCOUNT,
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(fs_group=1000),
                    # Keep Public Coder on the worker class that serves its local state PVC; do
                    # not allow it to fall back onto control-plane nodes.
                    affinity=k8s.Affinity(
                        node_affinity=k8s.NodeAffinity(
                            required_during_scheduling_ignored_during_execution=k8s.NodeSelector(
                                node_selector_terms=[
                                    k8s.NodeSelectorTerm(
                                        match_expressions=[
                                            k8s.NodeSelectorRequirement(
                                                key="kubernetes.io/hostname", operator="In", values=["ovh-ns102453"]
                                            )
                                        ]
                                    )
                                ]
                            )
                        )
                    ),
                    # The Secret comes from public_coder_proxy's ExternalSecret, rendered into this
                    # same Flux Kustomization through app/kustomization.yaml's ../proxy.
                    image_pull_secrets=[k8s.LocalObjectReference(name=forgejo_images.SECRET_NAME)],
                    init_containers=[
                        k8s.Container(
                            name="seed-config",
                            image=_IMAGE,
                            command=["sh", "-c", _SEED_CONFIG_SCRIPT],
                            volume_mounts=[
                                k8s.VolumeMount(name="cfg", mount_path="/cfg"),
                                k8s.VolumeMount(name="data", mount_path="/state"),
                            ],
                        )
                    ],
                    containers=[_openclaw_container(), public_coder_egress.relay_container()],
                    volumes=[
                        public_coder_egress.token_volume(),
                        k8s.Volume(
                            name="data",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_STATE_CLAIM_NAME),
                        ),
                        k8s.Volume(
                            name="kubeconfig", config_map=k8s.ConfigMapVolumeSource(name=_KUBECONFIG_CONFIG_MAP_NAME)
                        ),
                        k8s.Volume(name="cfg", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP_NAME)),
                        k8s.Volume(
                            name="trust", config_map=k8s.ConfigMapVolumeSource(name=public_coder_egress.CA_BUNDLE_NAME)
                        ),
                        # 0440 rather than 0400: fsGroup makes these root:1000, so owner-only would
                        # be unreadable by the container's own uid. OpenSSH's "unprotected private
                        # key" check only fires on a key the caller owns, and this one is
                        # root-owned, so group-readable is accepted.
                        k8s.Volume(
                            name="ssh",
                            projected=k8s.ProjectedVolumeSource(
                                default_mode=0o440,
                                sources=[
                                    k8s.VolumeProjection(config_map=k8s.ConfigMapProjection(name=_SSH_CONFIG_MAP_NAME)),
                                    k8s.VolumeProjection(
                                        secret=k8s.SecretProjection(name="public-coder-agent-devbox-ssh-key")
                                    ),
                                ],
                            ),
                        ),
                        k8s.Volume(name="tmp", empty_dir=k8s.EmptyDirVolumeSource()),
                        k8s.Volume(
                            name="diagnostics",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(
                                claim_name=_DIAGNOSTICS_CLAIM_NAME
                            ),
                        ),
                    ],
                ),
            ),
        ),
    )


def _claims(scope: Construct) -> None:
    k8s.KubePersistentVolumeClaim(
        scope,
        "state-v2",
        metadata=k8s.ObjectMeta(
            name=_STATE_CLAIM_NAME,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Worker-local replacement for the archived Public Coder OpenClaw state. "
                    "WaitForFirstConsumer binds it to ovh-ns102453 when the one-off restore Pod is created; "
                    "it must not be mounted by the Deployment before restore."
                )
            },
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="local-path-ovh-hdd",
            # local-path does not enforce PVC requests as quotas; this request leaves headroom for
            # the durable agent workspace and normal growth.
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("80Gi")}),
        ),
    )
    # Node's diagnostic output for the gateway: heap snapshots and fatal-error reports, written
    # here by the --diagnostic-dir/--report-directory flags in openclaw/default.nix.
    #
    # A claim rather than the /tmp emptyDir those flags used to target. An emptyDir survives a
    # container restart but dies with the Pod, and Flux rolls a new image every few hours -- so a
    # capture taken to investigate a slow leak would be collected by the very next roll, which is
    # the case it exists for.
    #
    # local-path rather than the seaweedfs-ovh default (cluster/README.md § Storage Selection):
    # that default buys reschedulability, and this Pod cannot reschedule -- the Deployment pins
    # it to ovh-ns102453 by required nodeAffinity and its state claim is local-path on that node.
    # A replicated claim would add a second, networked dependency to the gateway's startup path,
    # since a volume that will not mount blocks the Pod, in exchange for mobility it does not
    # have. RWO is no obstacle to reading a snapshot out: a throwaway Pod on the same node mounts
    # it alongside the gateway.
    #
    # Deliberately outside the VolSync backup set (public_coder/backup.py, which covers the state
    # claim): disposable investigation artifacts.
    k8s.KubePersistentVolumeClaim(
        scope,
        "diagnostics",
        metadata=k8s.ObjectMeta(
            name=_DIAGNOSTICS_CLAIM_NAME,
            namespace=NAMESPACE,
            annotations={"description": "Heap snapshots and Node diagnostic reports for the OpenClaw gateway"},
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="local-path-ovh-hdd",
            # A snapshot is roughly heap-sized and the heap is capped at 2 GiB, so this holds
            # several plus the reports. Snapshots are read in pairs and deleted once analysed;
            # local-path does not enforce the request as a quota.
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("20Gi")}),
        ),
    )


def _service(scope: Construct) -> None:
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=_SERVICE.name, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(selector=_SERVICE.pods.selector, ports=[_SERVICE.port.k8s_service_port()]),
    )


def _peer(namespace: str, pod_labels: dict[str, str]) -> k8s.NetworkPolicyPeer:
    return k8s.NetworkPolicyPeer(
        namespace_selector=k8s.LabelSelector(match_labels={"kubernetes.io/metadata.name": namespace}),
        pod_selector=k8s.LabelSelector(match_labels=pod_labels),
    )


def _tcp(port: int) -> k8s.NetworkPolicyPort:
    return k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(port), protocol="TCP")


def _network_policies(scope: Construct) -> None:
    # The egress fence. This is the enforcement layer; the HTTP_PROXY variables in the Deployment
    # are convenience only -- an agent that unsets them does not gain egress, it loses its only
    # route out.
    k8s.KubeNetworkPolicy(
        scope,
        "egress",
        metadata=k8s.ObjectMeta(name="egress", namespace=NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=_SERVICE.pods.selector),
            policy_types=["Egress"],
            egress=[
                # Scoped to kube-dns specifically: an unscoped port-53 rule lets the agent tunnel
                # arbitrary payloads to any public resolver, which is egress the proxy allowlist
                # never sees.
                k8s.NetworkPolicyEgressRule(
                    to=[_peer("kube-system", {"k8s-app": "kube-dns"})],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(53), protocol="UDP"), _tcp(53)],
                ),
                k8s.NetworkPolicyEgressRule(
                    to=[_peer(public_coder_egress.GATEWAY.pods.namespace, public_coder_egress.GATEWAY.pods.selector)],
                    ports=[_tcp(public_coder_egress.GATEWAY.pod_port)],
                ),
                # `ssh devbox`. Deliberately the piper and not the devbox itself: without a route
                # to port 22 on the VM, terminating at the piper is the only way through, which is
                # what makes the credential split enforced rather than advisory -- the same
                # argument as the proxy above.
                k8s.NetworkPolicyEgressRule(
                    to=[
                        k8s.NetworkPolicyPeer(
                            pod_selector=k8s.LabelSelector(match_labels=public_coder_sshpiper.SERVICE.pods.selector)
                        )
                    ],
                    ports=[_tcp(public_coder_sshpiper.SERVICE.pod_port)],
                ),
                k8s.NetworkPolicyEgressRule(
                    to=[_peer("litellm", {"app.kubernetes.io/name": "litellm"})], ports=[_tcp(4000)]
                ),
            ],
        ),
    )
    # Required for any trusted-proxy backend: without it, any pod in the cluster could set
    # x-authentik-username and be trusted as agentydragon. Template and rationale:
    # cluster/docs/sso.md, "Proxy-mode NetworkPolicy (required)".
    k8s.KubeNetworkPolicy(
        scope,
        "ingress",
        metadata=k8s.ObjectMeta(name="ingress", namespace=NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=_SERVICE.pods.selector),
            policy_types=["Ingress"],
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        _peer(
                            "authentik",
                            {"app.kubernetes.io/component": "server", "app.kubernetes.io/instance": "authentik"},
                        )
                    ],
                    ports=[_tcp(_SERVICE.pod_port)],
                )
            ],
        ),
    )


def _credentials(scope: Construct) -> None:
    # The agentydragon-agent GitHub PAT. The agent has its own GitHub account; it opens pull
    # requests from its own forks, so this token needs no write access to any repository owned
    # by someone else.
    #
    # Consumed by the **egress proxy**, not by the agent. The agent container holds only a
    # placeholder, which the proxy swaps for this value on requests bound for GitHub.
    ExternalSecret(
        scope,
        "github-token",
        metadata=ApiObjectMetadata(name=public_coder_proxy.GITHUB_TOKEN.secret.name, namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data("github-agentydragon-agent", "token", secret_key=public_coder_proxy.GITHUB_TOKEN.key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )
    # Gateway auth normally arrives through the Authentik trusted proxy. OpenClaw subagents and
    # other backend clients instead call the gateway over its loopback WebSocket, so they never
    # carry Authentik's identity headers.
    #
    # OpenClaw's supported trusted-proxy configuration is a separate local password fallback
    # (OPENCLAW_GATEWAY_PASSWORD). Generate it once and retain it; rotating this Secret
    # deliberately restarts the agent and invalidates local clients until they read the
    # replacement value from the same environment.
    mint_bearer_secret(
        scope,
        "gateway-password",
        name=_GATEWAY_PASSWORD.name,
        namespace=_GATEWAY_PASSWORD.namespace,
        generator_name=f"{_GATEWAY_PASSWORD.name}-generator",
        # A generated password is stable for the generator's lifetime. Avoid an automatic
        # rotation that would unnecessarily interrupt active sessions.
        refresh="8760h",
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        secret_type=None,
    )


def _role_ref(kind: str, name: str) -> k8s.RoleRef:
    return k8s.RoleRef(api_group=_RBAC_GROUP, kind=kind, name=name)


def _rbac(scope: Construct) -> None:
    # Namespace-scoped read-only Role for the public-coder-agent namespace.
    #
    # Deliberately narrow to start: the agent can observe its own deployment, pods, config,
    # events, and the resources that commonly fail in ways that are visible here (PVCs, network
    # policies, the proxy deployment). No secrets, no exec, no write verbs.
    #
    # Expand by adding RoleBindings in other namespaces that reference the deploy-owned
    # access-profile group, or by graduating to a ClusterRole when the scope warrants it.
    reader = "public-coder-agent-reader"
    k8s.KubeRole(
        scope,
        "reader",
        metadata=k8s.ObjectMeta(
            name=reader,
            namespace=NAMESPACE,
            annotations={"description": "Read-only diagnostic access for the public-coder access profile."},
        ),
        rules=[
            k8s.PolicyRule(
                api_groups=[""],
                resources=[
                    "pods",
                    "pods/log",
                    "events",
                    "services",
                    "configmaps",
                    "persistentvolumeclaims",
                    "serviceaccounts",
                ],
                verbs=_READ,
            ),
            k8s.PolicyRule(
                api_groups=["apps"], resources=["deployments", "statefulsets", "daemonsets", "replicasets"], verbs=_READ
            ),
            k8s.PolicyRule(api_groups=["networking.k8s.io"], resources=["networkpolicies", "ingresses"], verbs=_READ),
            k8s.PolicyRule(api_groups=[_RBAC_GROUP], resources=["roles", "rolebindings"], verbs=_READ),
            # Flux kustomizations and sources -- useful for diagnosing reconciliation.
            k8s.PolicyRule(api_groups=["kustomize.toolkit.fluxcd.io"], resources=["kustomizations"], verbs=_READ),
            k8s.PolicyRule(
                api_groups=["source.toolkit.fluxcd.io"], resources=["gitrepositories", "ocirepositories"], verbs=_READ
            ),
            # Pod metrics (metrics.k8s.io) -- nodes_top / pods_top equivalents.
            k8s.PolicyRule(api_groups=["metrics.k8s.io"], resources=["pods"], verbs=["get", "list"]),
            # The devbox is the public-coder agent's ephemeral build machine. Restrict
            # image-rollout inspection to this one named KubeVirt VM/VMI. The rollout
            # controller, not the agents, owns restarts.
            k8s.PolicyRule(
                api_groups=["kubevirt.io"],
                resources=["virtualmachines"],
                resource_names=["public-coder-devbox"],
                verbs=["get"],
            ),
            k8s.PolicyRule(
                api_groups=["kubevirt.io"],
                resources=["virtualmachineinstances"],
                resource_names=["public-coder-devbox"],
                # A named VMI waiter establishes its resource version with a list+watch;
                # resourceNames requires the caller to field-select this exact VMI.
                verbs=["get", "list", "watch"],
            ),
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "reader-binding",
        metadata=k8s.ObjectMeta(
            name=reader,
            namespace=NAMESPACE,
            annotations={"description": "Binds public-coder and its Haku superset to the reader Role."},
        ),
        role_ref=access.role_ref("public-coder-agent-reader"),
        subjects=[subject.k8s() for subject in access.profile_subjects("public-coder-agent-reader")],
    )

    # Revoke agent-driven restarts: the image rollout controller owns them now.
    # Keep an empty Role until old managed Sandboxes with this snapshotted grant
    # have been replaced. The reconciler reads the Role even for existing bindings;
    # deleting it immediately would mark those Sandboxes unready. No new selection
    # or static binding points here, and old bindings now confer no permissions.
    k8s.KubeRole(
        scope,
        "devbox-vmi-restart",
        metadata=k8s.ObjectMeta(name="public-coder-agent-devbox-vmi-restart", namespace=NAMESPACE),
        rules=[],
    )

    # Additional secret-free status for the public-coder workload itself.
    diagnostics = "agent-public-coder-extended-diagnostics-reader"
    k8s.KubeRole(
        scope,
        "extended-diagnostics-reader",
        metadata=k8s.ObjectMeta(
            name=diagnostics,
            namespace=NAMESPACE,
            annotations={"description": "Read-only VolSync backup status for Haku and public-coder."},
        ),
        rules=[
            k8s.PolicyRule(
                api_groups=["volsync.backube"], resources=["replicationsources", "replicationdestinations"], verbs=_READ
            )
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "extended-diagnostics-reader-binding",
        metadata=k8s.ObjectMeta(
            name=diagnostics,
            namespace=NAMESPACE,
            annotations={"description": "Binds Haku and public-coder to VolSync status."},
        ),
        role_ref=access.role_ref("public-coder-volsync-status"),
        subjects=[subject.k8s() for subject in access.profile_subjects("public-coder-volsync-status")],
    )

    # Use a new testing-only Role name rather than expanding the old staging+testing
    # reader's subjects: independently reconciled managed bindings must never acquire
    # the old staging credential while waiting for this chart to narrow its rules.
    acceptance = "agentplane-testing-login-reader"
    k8s.KubeRole(
        scope,
        "agentplane-testing-login-reader",
        metadata=k8s.ObjectMeta(name=acceptance, namespace=NAMESPACE),
        rules=[
            k8s.PolicyRule(
                api_groups=[""],
                resources=["secrets"],
                resource_names=["agentplane-testing-acceptance-operator"],
                verbs=["get"],
            )
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "agentplane-testing-login-reader-binding",
        metadata=k8s.ObjectMeta(name=acceptance, namespace=NAMESPACE),
        role_ref=access.role_ref("agentplane-testing-login"),
        subjects=[subject.k8s() for subject in access.profile_subjects("agentplane-testing-login")],
    )

    # Cluster-scoped node inventory for scheduling and health diagnostics.
    #
    # Nodes are cluster infrastructure rather than Git-managed application state, so this is
    # intentionally the sole cluster-wide grant for the public coder. In particular, it does
    # not include nodes/proxy, node logs, metrics, or any write verbs.
    node_reader = "public-coder-agent-node-reader"
    k8s.KubeClusterRole(
        scope,
        "node-reader",
        metadata=k8s.ObjectMeta(
            name=node_reader,
            annotations={"description": "Read-only node inventory for public-coder-agent diagnostics."},
        ),
        rules=[k8s.PolicyRule(api_groups=[""], resources=["nodes"], verbs=_READ)],
    )
    k8s.KubeClusterRoleBinding(
        scope,
        "node-reader-binding",
        metadata=k8s.ObjectMeta(
            name=node_reader,
            annotations={"description": "Binds public-coder and its Haku superset to read-only node inventory."},
        ),
        role_ref=access.role_ref("public-coder-node-read"),
        subjects=[subject.k8s() for subject in access.profile_subjects("public-coder-node-read")],
    )

    # Narrow cluster-scoped infrastructure metadata for public-coder.
    #
    # Haku already receives these resources through cluster-diagnostics-reader, and is repeated
    # here to make the Haku-superset invariant explicit. Keep public-coder on this smaller
    # surface rather than binding it to that broad role, which also exposes all Pods, PVs, RBAC,
    # webhooks, and node proxy endpoints.
    metadata_reader = "public-coder-agent-cluster-metadata-reader"
    k8s.KubeClusterRole(
        scope,
        "cluster-metadata-reader",
        metadata=k8s.ObjectMeta(
            name=metadata_reader,
            annotations={"description": "Read-only CRD schemas and node metrics for public-coder diagnostics."},
        ),
        rules=[
            k8s.PolicyRule(api_groups=["apiextensions.k8s.io"], resources=["customresourcedefinitions"], verbs=_READ),
            k8s.PolicyRule(api_groups=["metrics.k8s.io"], resources=["nodes"], verbs=["get", "list"]),
        ],
    )
    k8s.KubeClusterRoleBinding(
        scope,
        "cluster-metadata-reader-binding",
        metadata=k8s.ObjectMeta(
            name=metadata_reader,
            annotations={
                "description": "Binds public-coder and its Haku superset to CRD schemas and node metrics only."
            },
        ),
        role_ref=access.role_ref("public-coder-cluster-metadata-read"),
        subjects=[subject.k8s() for subject in access.profile_subjects("public-coder-cluster-metadata-read")],
    )

    # Static maximum execution ceiling for operator-approved Haku grants.
    #
    # This ClusterRoleBinding does not give the Agent standing Kubernetes access. Haku Console
    # still evaluates every request against the fixed standing SAR group or an active
    # Agent-owned grant before haku-kube-api-proxy sends it upstream. The personal-cluster
    # deployment deliberately makes that inline authorization boundary authoritative so future
    # useful grants do not each require a separate GitOps RBAC expansion.
    k8s.KubeClusterRoleBinding(
        scope,
        "cluster-admin-ceiling",
        metadata=k8s.ObjectMeta(
            name="haku-kube-api-proxy-cluster-admin-ceiling",
            annotations={"description": "Gives only the Haku authorization proxy a cluster-admin execution ceiling."},
        ),
        role_ref=_role_ref("ClusterRole", "cluster-admin"),
        subjects=[k8s.Subject(kind="ServiceAccount", name=kube_api_proxy.NAME, namespace=console.NAMESPACE)],
    )


def namespace_chart(app: App) -> Chart:
    """The namespace shared by the public-coder-agent components, and its `default` ServiceAccount.

    The ServiceAccount carries the pull secret for ducktape-ci, a private tenant in the in-cluster
    Forgejo registry; cluster/k8s/forgejo-images reflects `forgejo-images-creds` into this namespace.
    Workloads that don't set their own imagePullSecrets (the devbox VM's containerDisk pull) need it.
    """
    chart = Chart(app, "namespace", disable_resource_name_hashes=True)
    namespace = namespaces.namespace(
        chart, "namespace", name=NAMESPACE, vpa=Vpa.AUTO, labels={"name": NAMESPACE}, annotations=_NAMESPACE_ANNOTATIONS
    )
    k8s.KubeServiceAccount(
        chart,
        "default-service-account",
        metadata=k8s.ObjectMeta(name="default", namespace=namespace.name),
        image_pull_secrets=[k8s.LocalObjectReference(name="forgejo-images-creds")],
    )
    return chart


def app_chart(app: App) -> Chart:
    """The OpenClaw workload, its credentials, storage, network policy and RBAC."""
    workload = Chart(app, _NAME, disable_resource_name_hashes=True)
    _credentials(workload)
    _claims(workload)
    _deployment(workload)
    _service(workload)
    _network_policies(workload)
    _rbac(workload)
    return workload


def write_manifests(root: Path) -> None:
    write_charts(
        root, f"{HAND_WRITTEN_ROOT}/agents/public-coder-agent/app", namespace_chart, chart, kubeconfig_chart, app_chart
    )
