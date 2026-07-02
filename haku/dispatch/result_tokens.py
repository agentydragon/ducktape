"""Per-job result-submission tokens: HMAC(secret, job_id), deterministic.

The token authorizes exactly one thing — POST /jobs/<id>/result for its own job
id — so a prompt-injected worker can at worst fabricate its own job's result,
which is already within its power. Deterministic derivation (same pattern as
props/orchestration/agent_credentials.py) means no token storage: verification
recomputes and compares.
"""

import hashlib
import hmac


def mint(secret: str, job_id: str) -> str:
    return hmac.new(secret.encode(), job_id.encode(), hashlib.sha256).hexdigest()


def verify(secret: str, job_id: str, presented: str) -> bool:
    return hmac.compare_digest(mint(secret, job_id), presented)
