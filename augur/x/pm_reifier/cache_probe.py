"""Probe whether z.ai prompt caching matches a common token PREFIX *within* a single message, or only
caches whole identical messages / message-boundary prefixes. The windowed/kernel schemes rebuild a
growing history inside the first message, so within-message prefix caching is what makes them cheap.

Method: a long stable prefix P (~5k tokens) inside one user message, then a short differing suffix.
  A1 = P + suffix_A   (populate cache)
  A2 = P + suffix_A   (identical -> confirms caching is on at all)
  B  = P + suffix_B   (shared prefix, different suffix -> within-message prefix caching iff cached>0)
Reads usage.prompt_tokens_details.cached_tokens. Run: python3 augur/x/pm_reifier/cache_probe.py
"""

from __future__ import annotations

import json
import os
import pathlib
import urllib.request

KEY = (os.environ.get("ZAI_API_KEY") or pathlib.Path("/tmp/zai_key").read_text()).strip()
ENDPOINT = "https://api.z.ai/api/coding/paas/v4/chat/completions"
MODEL = "glm-4.7"

# ~12k-token stable prefix (identical across all calls); the line index is part of the fixed text.
PREFIX = "\n".join(
    f"context line {i:04d}: stable padding tokens alpha bravo charlie delta echo foxtrot" for i in range(600)
)


def ask(suffix: str, tag: str) -> None:
    body = {
        "model": MODEL,
        "thinking": {"type": "disabled"},
        "max_tokens": 5,
        "messages": [{"role": "user", "content": f"{PREFIX}\n\n{suffix}"}],
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    usage = json.loads(urllib.request.urlopen(req, timeout=60).read())["usage"]
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens")
    print(f"  {tag:18} prompt_tokens={usage.get('prompt_tokens')}  cached_tokens={cached}")


def main() -> None:
    print(f"prefix ~{len(PREFIX) // 4} tokens; model={MODEL}")
    ask("QUESTION A: reply with the single word alpha.", "A1 (populate)")
    ask("QUESTION A: reply with the single word alpha.", "A2 (identical)")
    ask("QUESTION B: reply with the single word bravo.", "B (shared prefix)")


if __name__ == "__main__":
    main()
