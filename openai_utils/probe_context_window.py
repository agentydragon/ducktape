"""Measure the real context window of a model, by binary search against the live API.

Written because every cheaper source of truth disagreed or was silent. LiteLLM
carries no `max_input_tokens` for the `codex-*` routes, the values hand-written
into the OpenClaw configs were inherited guesses, and published figures describe
the Codex *product* rather than this serving path (raw model ~1.05M, Codex CLI
caps well below it). What matters for a harness config is what this chain --
OpenClaw -> LiteLLM -> CLIProxyAPI -> upstream -- accepts without erroring, and
the only way to learn that is to ask it.

Two measurement traps this deliberately avoids, both of which produced wrong
numbers before they were understood:

* **Prompt caching.** Repeated filler makes successive probes share a prefix,
  the upstream serves it from cache, and `usage.input_tokens` comes back far
  below what was sent -- which reads exactly like truncation. Every probe here
  starts with a unique nonce, so no prefix is ever reusable.
* **Filler token density.** Unique-per-word filler defeats the cache but
  tokenizes several tokens per word, so a "360k" probe is really much larger and
  fails for the wrong reason. The filler here is a fixed five-common-word unit
  that tokenizes ~1 token/word, and `--calibrate` reports the measured ratio so
  the assumption is checked rather than trusted.

Truncation is detected rather than assumed: on success the reported
`input_tokens` must be within tolerance of what was sent. A silent truncation
somewhere in the chain would show up as a large negative delta, and is reported
as TRUNCATED rather than counted as a pass.
"""

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Five common words, ~1 token each, so token count is predictable from length.
FILLER_UNIT = "lorem ipsum dolor sit amet "
FILLER_UNIT_TOKENS = 5

# Fixed overhead of the envelope (role wrapper, trailing instruction). Measured,
# not assumed -- `--calibrate` prints it.
TOLERANCE = 0.02


@dataclass(frozen=True)
class Probe:
    sent: int
    status: int
    input_tokens: int | None
    error: str | None

    @property
    def accepted(self) -> bool:
        return self.status == 200

    @property
    def truncated(self) -> bool:
        """A 200 whose echoed token count is far below what was sent."""
        if not self.accepted or self.input_tokens is None:
            return False
        return self.input_tokens < self.sent * (1 - TOLERANCE)


def probe(base_url: str, api_key: str, model: str, tokens: int, timeout: float) -> Probe:
    nonce = f"probe-{time.time_ns()}-{os.getpid()}"
    filler = FILLER_UNIT * max(1, tokens // FILLER_UNIT_TOKENS)
    content = f"{nonce}\n{filler}\nReply OK."
    body = json.dumps({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": content}]}).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/messages",
        data=body,
        headers={"content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
            return Probe(tokens, resp.status, payload.get("usage", {}).get("input_tokens"), None)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        return Probe(tokens, e.code, None, detail[:300])


def find_limit(
    base_url: str, api_key: str, model: str, low: int, high: int, precision: int, timeout: float
) -> tuple[int, int]:
    """Return (largest accepted, smallest rejected), narrowed to `precision`."""
    first = probe(base_url, api_key, model, low, timeout)
    if first.truncated:
        raise RuntimeError(
            f"{model}: truncation at the floor ({low} sent, {first.input_tokens} counted) -- "
            "something in the chain is silently dropping input, so no limit here is meaningful."
        )
    if not first.accepted:
        raise RuntimeError(f"{model}: rejected at the floor ({low}): {first.error}")

    while high - low > precision:
        mid = (low + high) // 2
        p = probe(base_url, api_key, model, mid, timeout)
        if p.truncated:
            raise RuntimeError(
                f"{model}: TRUNCATED at {mid} (counted {p.input_tokens}) -- "
                "a pass here would be an artifact, not a real limit."
            )
        logger.info(
            "%s %7d -> %s%s",
            model,
            mid,
            "accept" if p.accepted else "reject",
            f" (counted {p.input_tokens})" if p.input_tokens else "",
        )
        low, high = (mid, high) if p.accepted else (low, mid)
    return low, high


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("models", nargs="+", help="model ids to probe")
    ap.add_argument("--base-url", default="http://litellm.litellm.svc.cluster.local:4000")
    ap.add_argument("--api-key-env", default="OPENCLAW_LITELLM_API_KEY")
    ap.add_argument("--low", type=int, default=100_000, help="known-good floor")
    ap.add_argument("--high", type=int, default=1_100_000, help="known-bad ceiling")
    ap.add_argument("--precision", type=int, default=2_000)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--calibrate", action="store_true", help="report filler token density and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    api_key = os.environ[args.api_key_env]

    if args.calibrate:
        for model in args.models:
            p = probe(args.base_url, api_key, model, 1000, args.timeout)
            logger.info(
                "%s: sent~1000 -> counted %s (ratio %.4f, overhead %s)",
                model,
                p.input_tokens,
                (p.input_tokens or 0) / 1000,
                (p.input_tokens or 0) - 1000,
            )
        return

    for model in args.models:
        accepted, rejected = find_limit(
            args.base_url, api_key, model, args.low, args.high, args.precision, args.timeout
        )
        logger.info("RESULT %s: accepted %d, rejected %d", model, accepted, rejected)


if __name__ == "__main__":
    main()
