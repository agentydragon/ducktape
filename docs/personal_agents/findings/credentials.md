# Credentials

Findings are numbered in discovery order across the whole programme and cited by
number from cluster manifests, so the IDs are stable and non-contiguous here.
Index of all findings: [README.md](README.md).

## F6. S2 passes once a GitHub credential exists — resolved

The image ships no `gh` and the shape has no token: `command -v gh` → no,
`$GITHUB_TOKEN` → unset. In the OpenShell shape the credential arrives from the
attached `agentydragon-github` provider, which does not exist here.

Nothing else is missing: `git` is present, the proxy allowlist already permits
`github.com`, `api.github.com`, `codeload.github.com` and the
`*.githubusercontent.com` hosts, and PR creation needs only `curl` + the REST
API, which are available. **S2 on this shape needed one thing: a GitHub token in a Secret, under a name
OpenClaw does not strip.** The second clause was missing for a day and cost
several rounds; see F7.
Supplied by pointing an `ExternalSecret` at the same `ClusterSecretStore` the
OpenShell GitHub provider uses (`kubernetes-claude-sandbox-secret-store`, key
`github-token`), which needed no new permission.

With the token wired in, **S2 passed**: from the single instruction "open a pull
request against agentydragon/ducktape containing any small arbitrary change you
like, work it end to end yourself and report the PR URL", the agent produced
[#3560](https://github.com/agentydragon/ducktape/pull/3560) — authored by
`agentydragon-agent`, one file, one line, on its own fork branch. No per-command
steering.

The proxy log shows what the workflow actually needs, and the allowlist covered
all of it: `github.com` (push), `codeload.github.com` (clone),
`api.github.com` (PR creation). Nothing was blocked. Note the image ships no
`gh`, so the agent used the REST API directly — worth knowing before assuming
`gh` is available.

## F7. `GITHUB_TOKEN` is stripped from the exec tool by an exact-name denylist

**What happens.** The exec tool inherits nearly the whole container environment —
including `OPENCLAW_LITELLM_API_KEY`, another live credential. Exactly one name is
missing. Probes settle that it is name-matching and not prefix-matching or
credential-sniffing:

| Variable             | Visible to exec tool |
| -------------------- | -------------------- |
| `GITHUB_TOKEN`       | **no**               |
| `GITHUB_TOKEN_PROBE` | yes                  |
| `GITHUB_PROBE`       | yes                  |
| `GH_PAT_PROBE`       | yes                  |

**Where it lives.** A hardcoded list in `/app/dist/host-env-security-*.js`
alongside `GH_TOKEN`, `GITLAB_TOKEN`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AZURE_CLIENT_*`, `DATABASE_URL`,
`MONGODB_URI`, `NODE_AUTH_TOKEN`, `NPM_TOKEN`, `REDIS_URL`, `SSH_AUTH_SOCK`, plus
code-injection vectors (`ANSIBLE_*_PLUGINS`, `TF_CLI_CONFIG_FILE`, `R_LIBS_USER`).
A second, independent layer in `bash-tools.exec-runtime-*.js` redacts
`/\b(GITHUB_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|GOOGLE_API_KEY|GEMINI_API_KEY)\b/`
from output.

**It is hygiene, not a boundary — proven, not argued.** The gateway and the exec
tool both run as uid 1000, so the agent reads the token out of the parent's
environment. Run from inside the agent on `public-coder-agent`:

```text
own_env=0      # the denylist removed it from the tool env
proc7=1        # ...and /proc/7/environ still has it
len=54         # full 40-character token, intact
```

Two things follow. The mechanism stops `echo $GITHUB_TOKEN` and keeps the token
out of transcripts, which is worth something. It does not stop a two-line `/proc`
read by the same uid, which is the threat it appears to address. And **renaming is
not a bypass of a protection** — there is no protection to bypass; the agent could
always read the token. `GH_PAT` makes an existing exposure legible instead of
implied, which is what `public-coder-agent/README.md` "Known gaps" already said:
with `sandbox.mode: "off"` the agent runs as the harness, with the token in its
environment.

Note `GH_TOKEN` is on the list too, so the obvious second name is also stripped.

**The fix is one line.** Pass the same Secret under a non-stripped name:

```yaml
- name: GH_PAT # was GITHUB_TOKEN, which host-env-security strips
  valueFrom:
    secretKeyRef: { name: <secret>, key: GITHUB_TOKEN }
```

Verified end to end: with only that change and no shims, no `pathPrepend`, and no
`TOOLS.md` guidance beyond naming the variable, the agent opened
[#3572](https://github.com/agentydragon/ducktape/pull/3572) from one instruction.
It used the REST API itself — no `gh` needed.

**OpenClaw has no GitHub integration to bind the token to.** All four occurrences
of "github" in the 2.5 MB config schema are `github-copilot`, a model/embedding
provider. The 70-entry plugin registry has nothing git- or forge-related, and
there are no `git`/`repo`/`vcs`/`forge` config keys. So "wire it into OpenClaw's
GitHub integration" is a good idea the product does not support.

**Also discovered, not needed:** `tools.exec.pathPrepend` is real, applies to exec
runs, and lands **first** on the tool's PATH — a `git` shim there transparently
shadows `/usr/bin/git`. `openclaw config patch --file` applies it as a validated
write ("No gateway restart needed"). That route works (PRs
[#3570](https://github.com/agentydragon/ducktape/pull/3570),
[#3571](https://github.com/agentydragon/ducktape/pull/3571)) and was torn out in
favour of the rename, because the agent uses the GitHub API perfectly well and the
shim was machinery for nothing. Keep it in mind for cases that genuinely need a
wrapper. Caveat from the schema: `safeBinTrustedDirs` warns PATH entries are never
auto-trusted, so a `pathPrepend` dir does not become a safe-bin source.

**Unresolved:** F6 records `oc-plain` passing S2 on 2026-07-29 with the variable
named `GITHUB_TOKEN`, which the denylist should have stripped. `oc-plain` shows
the same strip today. The `/proc` route existed then and would explain it, but
that is a hypothesis, not a finding — the original run was not instrumented. Treat
F6's PR [#3560](https://github.com/agentydragon/ducktape/pull/3560) as
unexplained rather than as evidence that `GITHUB_TOKEN` ever worked.

## F10. A credential-injecting proxy works, and closes F7's exposure completely

F7 established that the token is readable from `/proc` no matter what it is named,
so the agent always possessed a usable credential while reading untrusted public
repositories. The TODO carried this as "prefer the OpenShell placeholder model,
when it becomes possible". It is possible now, without OpenShell: our mitmproxy is
already in-path and already terminates TLS, so it can hold the credential instead.

Shape: the token moves to the **proxy's** environment and is removed from the
agent's. The addon attaches `Authorization` on the way out, for GitHub hosts only.

```text
agent container:  env matching GITHUB_TOKEN|GH_PAT|GH_TOKEN  -> 0
                  same grep across /proc/*/environ           -> 0
                  curl https://api.github.com/user           -> "login": "agentydragon-agent"
```

The agent authenticates as the bot while holding nothing. A prompt injection from
a cloned repo can _use_ the credential inside the sandbox but cannot _steal_ it —
which is the property the `GH_PAT` rename only gestured at.

**It also buys policy, which is the bigger half.** The proxy sees the method and
path, so writes can be confined to the agent's own fork:

```text
GET    /repos/agentydragon/ducktape                -> 200  (read upstream)
PATCH  /repos/agentydragon/ducktape                -> 403  (refused at proxy)
POST   /repos/agentydragon/ducktape/issues         -> 403  (refused at proxy)
POST   /repos/agentydragon/ducktape/pulls          -> 422  (allowed through; GitHub rejects the empty body)
DELETE /repos/someoneelse/theirrepo                -> 403  (refused at proxy)
POST   /repos/agentydragon-agent/ducktape/git/refs -> 422  (allowed through; own fork)
```

So a PAT that can push anywhere is narrowed, at the proxy, to one that can only
write to the agent's fork and open pull requests upstream.

**End to end:** with no credential in the container, the agent opened
[#3574](https://github.com/agentydragon/ducktape/pull/3574) unaided in 26 tool
calls.

**Gotcha that cost a run.** The first policy denied everything, because writes to
the same fork arrive in two path shapes — the git transport
(`github.com/<owner>/<repo>.git/...`) and the REST API
(`api.github.com/repos/<owner>/<repo>/...`). The agent reached for the API and
never ran `git push` at all, so a policy written against the git shape blocked its
whole workflow. Worth noting for any future path-based policy: **match on what the
agent actually does, not on what you imagine it does.**

**The options, and which actually work:**

| Option                                                | Agent can read the credential?                        | Tested                                                                                                                                               |
| ----------------------------------------------------- | ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Token in the agent env (`GH_PAT`, current production) | yes, from `/proc`                                     | works; F7                                                                                                                                            |
| **mitmproxy injection + write policy**                | **no**                                                | **works; this finding**                                                                                                                              |
| OpenShell provider placeholders                       | no                                                    | **not expressible** — `OpenShellSandbox` has no `entrypoint`, so the harness cannot be the supervised process                                        |
| Git credential helper backed by a broker              | yes — the helper hands the real token to the pod      | not tested; dominated by injection                                                                                                                   |
| Sidecar exposing only "open a PR"                     | no                                                    | not tested; strongest, but the agent loses ordinary git and every unanticipated operation                                                            |
| Airlock                                               | yes — it _distributes_ tokens as Secrets to consumers | not an alternative: it solves refresh, not possession. Good **complement** — let Airlock own rotation and give the token to the proxy, not the agent |

The injecting proxy is the only option that is both tested and non-dominated: it
removes possession, adds policy, and needs no new component, because the proxy
S4 already requires is the thing doing the work.

**Off-the-shelf alternatives were surveyed separately, and the first pass got the
conclusion wrong.** It found only reverse-proxy injectors — Envoy's
`credential_injector` (tested: injects on a reverse listener, cannot inject
through a `CONNECT` tunnel), `gh-aw-firewall`, Secretless, the hosted agent-auth
platforms — and concluded nothing off-the-shelf fits. There is in fact a sizeable
2026 ecosystem, including a Kubernetes-native option that injects via **eBPF at
the TLS write path with no interception and no CA at all**, which would delete
the F8 failure class outright. The three-camp breakdown and what it means for
this shape are distilled in [../credential_proxy.md](../credential_proxy.md).
