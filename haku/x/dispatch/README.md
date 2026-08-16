# Haku dispatch plane — dispatcher + worker

The ducktape-owned service that lets Haku (the orchestrator) hand well-scoped jobs to
cheaper, lower-trust worker agents running in per-provider **zones**, without ever
holding a credential for the worker's model provider. Haku can call this plane but not
modify it, so it structurally cannot bypass its own admission gate.

Design context and the not-yet-built roadmap: <../../archive/2026_08_multi_agent.md>. Security
contract (enforcement inventory rows): <../../docs/security.md>. Cluster wiring:
<../../../cluster/k8s/x/haku/dispatch/README.md>; zone perimeters:
<../../../cluster/k8s/x/haku/zones/README.md>.

## Request flow (`POST /jobs`)

`app.py` is the FastAPI app; one request threads these modules:

1. **Auth** — bearer `HAKU_API_TOKEN` (the only token Haku holds for this plane).
2. **Idempotency short-circuit** — the k8s Job name is `job-<sha256(idempotency_key)[:16]>`
   (`k8s_jobs.job_name`); a retried POST that already has a DB row returns it unchanged.
3. **Credential lint** (`prompt_lint.py`) — deterministic, zero-false-negative regex pass
   for pasted secrets (API keys, tokens, PEM, JWT). A hit rejects **before** the
   classifier runs, naming the credential kind.
4. **Classifier gate** (`classifier.py`) — one Anthropic call, verdict forced through a
   tool schema (`ClassifierVerdict`). The system prompt **is** the zone admission policy:
   it judges by the SHAPE and PROVENANCE of the content (personal-looking data, non-public
   provenance) because it cannot verify identities. `zai` admits only
   public-by-construction prompts. An optional operator-provided context file
   (`classifier_context_path`, mounted from a SOPS secret, never committed) sharpens it
   with private specifics. On reject the reason is logged; the caller gets 403.
5. **Per-job key mint** (`litellm_keys.py`) — a virtual key on the workers-LiteLLM scoped
   to the zone's model allowlist + `max_budget_usd` + a TTL. This key is the worker's only
   LLM credential; its allowlist is the zone boundary at the LLM layer.
6. **Job stamp** (`k8s_jobs.py`) — renders the reviewed Job template with explicit `${...}`
   token replacement (not `string.Template` — the template carries a literal `$imagepolicy`
   Flux marker) and creates the Job plus a same-named Secret (per-job key + HMAC result
   token) in the zone namespace. Job-name collisions (409) resolve to the winner; the API
   server arbitrates races.

`POST /jobs/<id>/result` verifies the per-job HMAC token (`result_tokens.py`) and stores
the blob; `DELETE /jobs/<id>` is the kill switch. **There is no GET surface** — Haku reads
job/result state directly through the read-only `haku_reader` Postgres role.

## Data model (`db.py`)

Two tables, SQLAlchemy ORM: `jobs(id, zone, model, prompt, status, created_at)` and
`results(job_id, result, exit_code, submitted_at)`. The prompt is Haku-authored and the
result worker-authored — no credential is ever stored. `JobRequest`/`JobRecord` and the
`ClassifierVerdict` schema live in `models.py`.

> **Deviation:** SQLAlchemy loads the `postgresql+asyncpg` driver dynamically, so
> `@pypi//asyncpg` is an explicit BUILD dep — tests run on `sqlite+aiosqlite` and cannot
> catch its absence (it shipped a startup `ModuleNotFoundError` once; see git history).

## Worker image (`worker/`)

One image (`ghcr.io/agentydragon/haku-zone-worker`, built by
`formerly built by .github/workflows/haku-zone-worker-image.yml`) carries both zone harnesses — **Claude
Code CLI** for the zai zone's Anthropic wire shape and **Codex CLI** for the future oai
zone's Responses shape — plus git and a stdlib-only `entrypoint.py`. It holds **no
credentials**; the per-job key and result token arrive only via the Job's Secret.

The entrypoint (contract in `worker/entrypoint.py`): read the prompt from the mounted
Secret, run the zone's harness headless in an empty `/workspace` (a job that needs a repo
is told in the prompt to `git clone` it — public GitHub only), then **after the harness
exits** POST `/output/result.md` + exit status to the dispatcher with the job-scoped
token. Missing result file → a descriptive placeholder, never silence.

## How Haku uses it

Submit from `haku-sandbox` (the dispatcher CNP admits that origin); token from the
reflected `dispatcher-haku-token` secret. Read results with the `haku_reader` role
(`haku-dispatch-db-haku-reader` secret). The operating runbook Haku follows lives in its
state repo (`memory/dispatch.md`), not here.

## Build & test

```bash
bbr test //haku/x/dispatch/...
```

`test_zones_config.py` parity-tests the zone model allowlists against the generated
workers-LiteLLM config; `test_k8s_jobs.py` renders the real Job template;
`test_prompt_lint.py` / `test_result_tokens.py` cover the deterministic layers.
