"""Credential-substitution integration tests, end to end through the embedded proxy (#4914).

The gate swaps an inert placeholder for the real credential inside ``match_headers`` before
forwarding (haku/egress/addon.py, #4670). These drills exercise the swap across grant shapes —
multiple substitutions in one request, per-header scoping, case and duplicate-header edges, the
base64 ``Basic`` re-encode git-over-HTTPS relies on — and the never-log invariant: neither the
placeholder nor the real value may ever reach the gate's logs.

Every test drives a real client through the real runner toward the recording upstream, so the
assertions are on what the upstream actually received, not on the addon in isolation.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest_bazel
from more_itertools import one

from haku.egress.decision import PlaceholderSubstitution
from haku.egress.testing.proxy_test_harness import (
    PLACEHOLDER,
    REAL_CREDENTIAL,
    RecordingUpstream,
    allow,
    bearer_substitution,
    capture_egress_logs,
    make_proxy,
    proxied_get,
    proxied_get_raw,
)
from haku.egress.testing.static_decide_client import StaticDecideClient


async def test_multiple_substitutions_each_reach_their_header(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """Two grant substitutions in one decision, each scoped to a different header, both applied."""
    api_key = PlaceholderSubstitution(
        placeholder="ph-api-key", value="real-api-key", match_headers=frozenset({"X-Api-Key"})
    )
    decide = StaticDecideClient(allow(bearer_substitution(), api_key))
    async with make_proxy(decide, tmp_path) as proxy:
        status, _ = await proxied_get(
            proxy,
            f"http://127.0.0.1:{upstream.port}/multi",
            headers={"Authorization": f"Bearer {PLACEHOLDER}", "X-Api-Key": "ph-api-key"},
        )
    assert status == 200
    recorded = one(upstream.requests)
    assert recorded.headers["authorization"] == f"Bearer {REAL_CREDENTIAL}"
    assert recorded.headers["x-api-key"] == "real-api-key"


async def test_repeated_placeholder_in_one_value_all_swapped(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """Replace semantics: every occurrence of the placeholder within a scanned value is swapped."""
    decide = StaticDecideClient(allow(bearer_substitution()))
    async with make_proxy(decide, tmp_path) as proxy:
        status, _ = await proxied_get(
            proxy,
            f"http://127.0.0.1:{upstream.port}/repeat",
            headers={"Authorization": f"{PLACEHOLDER} and again {PLACEHOLDER}"},
        )
    assert status == 200
    recorded = one(upstream.requests)
    assert recorded.headers["authorization"] == f"{REAL_CREDENTIAL} and again {REAL_CREDENTIAL}"
    assert PLACEHOLDER not in recorded.headers["authorization"]


async def test_same_placeholder_scanned_and_unscanned_header(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """One placeholder in two headers: swapped in the scanned header, verbatim in the other."""
    decide = StaticDecideClient(allow(bearer_substitution()))  # scans Authorization only
    async with make_proxy(decide, tmp_path) as proxy:
        status, _ = await proxied_get(
            proxy,
            f"http://127.0.0.1:{upstream.port}/scoped",
            headers={"Authorization": f"Bearer {PLACEHOLDER}", "X-Copy": f"Bearer {PLACEHOLDER}"},
        )
    assert status == 200
    recorded = one(upstream.requests)
    assert recorded.headers["authorization"] == f"Bearer {REAL_CREDENTIAL}"
    assert recorded.headers["x-copy"] == f"Bearer {PLACEHOLDER}"  # unscanned: placeholder rides through
    assert REAL_CREDENTIAL not in recorded.headers["x-copy"]


async def test_match_headers_case_insensitive(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """A lowercased match rule still swaps a differently-cased request header (HTTP names are case-insensitive)."""
    substitution = PlaceholderSubstitution(
        placeholder=PLACEHOLDER, value=REAL_CREDENTIAL, match_headers=frozenset({"authorization"})
    )
    decide = StaticDecideClient(allow(substitution))
    async with make_proxy(decide, tmp_path) as proxy:
        status, _ = await proxied_get_raw(
            proxy.listen_port,
            f"http://127.0.0.1:{upstream.port}/case",
            header_lines=[("AUTHORIZATION", f"Bearer {PLACEHOLDER}")],
        )
    assert status == 200
    recorded = one(upstream.requests)
    assert recorded.headers["authorization"] == f"Bearer {REAL_CREDENTIAL}"


async def test_duplicate_header_all_occurrences_swapped(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """A scanned header sent twice has the placeholder swapped in every occurrence."""
    substitution = PlaceholderSubstitution(
        placeholder=PLACEHOLDER, value=REAL_CREDENTIAL, match_headers=frozenset({"X-Token"})
    )
    decide = StaticDecideClient(allow(substitution))
    async with make_proxy(decide, tmp_path) as proxy:
        status, _ = await proxied_get_raw(
            proxy.listen_port,
            f"http://127.0.0.1:{upstream.port}/dup",
            header_lines=[("X-Token", f"first {PLACEHOLDER}"), ("X-Token", f"second {PLACEHOLDER}")],
        )
    assert status == 200
    recorded = one(upstream.requests)
    x_token_values = [value for name, value in recorded.header_lines if name == "x-token"]
    assert x_token_values == [f"first {REAL_CREDENTIAL}", f"second {REAL_CREDENTIAL}"]


async def test_basic_password_field_swapped_and_reencoded(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """The placeholder as the ``user:password`` password is swapped inside the decoded base64 and re-encoded."""
    decide = StaticDecideClient(allow(bearer_substitution()))
    presented = base64.b64encode(f"git:{PLACEHOLDER}".encode()).decode()
    async with make_proxy(decide, tmp_path) as proxy:
        status, _ = await proxied_get(
            proxy, f"http://127.0.0.1:{upstream.port}/basic", headers={"Authorization": f"Basic {presented}"}
        )
    assert status == 200
    expected = base64.b64encode(f"git:{REAL_CREDENTIAL}".encode()).decode()
    assert one(upstream.requests).headers["authorization"] == f"Basic {expected}"


async def test_basic_without_placeholder_untouched(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """A ``Basic`` payload not containing the placeholder is forwarded byte-for-byte."""
    decide = StaticDecideClient(allow(bearer_substitution()))
    presented = base64.b64encode(b"git:some-other-secret").decode()
    async with make_proxy(decide, tmp_path) as proxy:
        status, _ = await proxied_get(
            proxy, f"http://127.0.0.1:{upstream.port}/basic-other", headers={"Authorization": f"Basic {presented}"}
        )
    assert status == 200
    assert one(upstream.requests).headers["authorization"] == f"Basic {presented}"


async def test_basic_non_base64_left_verbatim(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """A ``Basic`` value that is not valid base64 is not mangled (the binascii.Error branch)."""
    decide = StaticDecideClient(allow(bearer_substitution()))
    async with make_proxy(decide, tmp_path) as proxy:
        status, _ = await proxied_get(
            proxy, f"http://127.0.0.1:{upstream.port}/basic-bad", headers={"Authorization": "Basic not*base64!"}
        )
    assert status == 200
    assert one(upstream.requests).headers["authorization"] == "Basic not*base64!"


async def test_never_log_invariant_placeholder_and_value_absent(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """The gate logs the admission but never the placeholder or the real credential value (#4670)."""
    decide = StaticDecideClient(allow(bearer_substitution()))
    async with capture_egress_logs() as logs, make_proxy(decide, tmp_path) as proxy:
        status, _ = await proxied_get(
            proxy, f"http://127.0.0.1:{upstream.port}/logged", headers={"Authorization": f"Bearer {PLACEHOLDER}"}
        )
    assert status == 200
    assert one(upstream.requests).headers["authorization"] == f"Bearer {REAL_CREDENTIAL}"
    rendered = logs.rendered()
    assert "allow" in rendered  # the admission was logged...
    assert PLACEHOLDER not in rendered  # ...but the capability handle was not
    assert REAL_CREDENTIAL not in rendered  # ...and neither was the redeemed value


if __name__ == "__main__":
    pytest_bazel.main()
