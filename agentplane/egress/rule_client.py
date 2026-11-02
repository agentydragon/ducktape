"""Lightweight client for the workload's redacted `/v1/rules` snapshot.

This view is informational: the proxy remains the admission authority. Returned
placeholders are inert strings, never the substituted credentials.
"""

import os
import re
import ssl

import httpx
from pydantic import BaseModel, Field


class StringList(BaseModel):
    values: list[str] = Field(default_factory=list)


class Target(BaseModel):
    method: str
    header: str | None = None
    scheme: str | None = None


class Credential(BaseModel):
    name: str
    placeholder: str
    targets: list[Target]


class Rule(BaseModel):
    hosts: list[str]
    methods: StringList | None = None
    paths: StringList | None = None
    credential: Credential | None = None

    def matches(self, *, host: str, method: str, path: str) -> bool:
        host = host.lower()
        if not any(
            (pattern == "*" and bool(host))
            or (pattern.startswith("*.") and host.endswith(pattern[1:].lower()) and len(host) > len(pattern) - 1)
            or host == pattern.lower()
            for pattern in self.hosts
        ):
            return False
        if self.methods is not None and method.upper() not in {value.upper() for value in self.methods.values}:
            return False
        if self.paths is None:
            return True
        # Match the proxy's PathGlob: only '*' and '**' are special; ignore query.
        return any(
            re.fullmatch(
                "".join(
                    ".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
                    for part in re.split(r"(\*\*|\*)", pattern)
                ),
                (path or "/").split("?", 1)[0],
            )
            is not None
            for pattern in self.paths.values
        )


class Policy(BaseModel):
    name: str
    rules: list[Rule] = Field(default_factory=list)


class Rules(BaseModel):
    policies: list[Policy] = Field(default_factory=list)

    def find_credential(
        self,
        *,
        host: str,
        method: str,
        path: str,
        target_method: str,
        name: str | None = None,
        header: str = "Authorization",
        scheme: str | None = None,
    ) -> Credential:
        matches: list[Credential] = []
        for policy in self.policies:
            for rule in policy.rules:
                credential = rule.credential
                if (
                    credential is None
                    or (name is not None and credential.name != name)
                    or not rule.matches(host=host, method=method, path=path)
                ):
                    continue
                if (
                    any(
                        target.method == target_method
                        and target.header is not None
                        and target.header.lower() == header.lower()
                        and (scheme is None or (target.scheme is not None and target.scheme.lower() == scheme.lower()))
                        for target in credential.targets
                    )
                    and credential not in matches
                ):
                    matches.append(credential)
        if len(matches) != 1:
            raise LookupError(f"Expected one {name!r} credential for {method} {host}{path}; found {len(matches)}")
        return matches[0]


def load_rules(*, client: httpx.Client | None = None) -> Rules:
    url = os.environ.get("AGENTPLANE_EGRESS_URL", "http://egress.agentplane-staging.svc.cluster.local")
    credential = os.environ.get("AGENTPLANE_EGRESS_CREDENTIAL", "agentplane-credential-agentplane-workload")
    if client is None:
        with create_client(timeout=30) as owned_client:
            return _load_rules(owned_client, url=url, credential=credential)
    return _load_rules(client, url=url, credential=credential)


def _load_rules(client: httpx.Client, *, url: str, credential: str) -> Rules:
    response = client.get(f"{url.rstrip('/')}/v1/rules", headers={"Authorization": f"Bearer {credential}"})
    response.raise_for_status()
    return Rules.model_validate(response.json())


def create_client(*, timeout: float) -> httpx.Client:
    """Honor the sandbox CA bundle, including curl/requests-style configuration."""
    ca_bundle = (
        os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
    )
    return httpx.Client(timeout=timeout, verify=ssl.create_default_context(cafile=ca_bundle))
