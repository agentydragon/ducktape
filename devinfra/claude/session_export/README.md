# Claude Code session export

Downloads the full event log of every Claude Code cloud session (web, mobile, desktop-cloud, routines) on a
claude.ai account, archived sessions included, into a local archive.

No official export covers these: the account data export documents no Code sessions, and the Compliance API
[excludes Claude Code cloud sessions](https://platform.claude.com/docs/en/manage-claude/compliance-sessions).
This reads the private API `claude.ai/code` and the Claude Code CLI use, authenticated as you
([docs/api.md](docs/api.md)). It can break without notice, and the consumer terms may restrict automated access.

Read-only against the API (`GET` only). `export` writes files on your machine; `sync` writes only to the database
you give it.

## Run

Path arguments resolve against the directory `bb run` was invoked from.

### 1. Get a credential

**OAuth grant (preferred).** `pair` mints a grant dedicated to this tool, the way Claude Code logs in:

```bash
bb run //devinfra/claude/session_export:export_sessions_bin -- pair --credentials-file ~/.claude-session-export.json
```

Open the printed URL in a browser signed in to the account and approve. The browser is redirected to
`http://localhost:54545/callback`, so it must reach the machine running `pair`; otherwise forward the port
(`ssh -L 54545:localhost:54545 host`, or `kubectl port-forward` to a pod).

The 0600 credential file holds an access token (valid for 8 hours) that the tool refreshes itself. **Give the file
one owning process**: every refresh returns a new refresh token, which the tool writes back before using it, so
pointing this tool at `~/.claude/.credentials.json` would make it and Claude Code invalidate each other.

`--scope` (repeatable) narrows the grant; the default is `user:profile user:sessions:claude_code`. That is enough to
list sessions and read events, and the Messages API refuses such a grant, so it cannot spend inference quota. The
sessions scope very likely still permits creating and steering cloud sessions, so treat the file as a credential. If
the authorize page ever refuses the default, retry with the set Claude Code itself requests: `--scope user:profile
--scope user:inference --scope user:sessions:claude_code --scope user:mcp_servers --scope user:file_upload`.

**Cookie.** In a browser signed in to claude.ai: DevTools → Application → Cookies → `https://claude.ai`. Copy
`sessionKey` (HttpOnly, so `document.cookie` does not show it) and `lastActiveOrg`.

```bash
umask 077
read -rsp 'sessionKey: ' K; echo; read -rp 'lastActiveOrg: ' O
printf 'sessionKey=%s; lastActiveOrg=%s\n' "$K" "$O" > ~/.claude-ai-cookie; unset K O
```

Below, pass `--cookie-file ~/.claude-ai-cookie` in place of `--credentials-file`.

### 2. Size it (optional)

```bash
bb run //devinfra/claude/session_export:export_sessions_bin -- count --credentials-file ~/.claude-session-export.json
```

### 3. Export

```bash
bb run //devinfra/claude/session_export:export_sessions_bin -- export --credentials-file ~/.claude-session-export.json --out ~/claude-sessions
```

Measured on one account with the cookie: 1,531 sessions, 5.2 M events (median session 702, largest 275 k), 3.0 GB
gzipped, about 30 minutes at the default `--workers 3`. Disk is the constraint, not the network.

Resumable: rerun the same command after an interruption or failure. Finished sessions are skipped; a session is
exported again if it was live when exported or has new events since. The first error aborts the run.
`--limit-sessions N` and `--ids a,b` select a subset for a trial run.

### 4. Verify

```bash
bb run //devinfra/claude/session_export:export_sessions_bin -- verify --out ~/claude-sessions
```

Re-reads every file and fails, listing each problem, unless `sequence_num` runs `1..N` without a gap and `N`
matches the manifest for every session in the index. Needs a full export, not a subset. A session that was live
during the export is reported; rerun step 3 to catch up.

### 5. Retire the credential

```bash
shred -u ~/.claude-session-export.json   # or ~/.claude-ai-cookie
```

For the cookie, also log out of all devices (claude.ai → Settings → Account), which invalidates the `sessionKey`.
No way to revoke an OAuth grant is known; whether logging out of all devices does is untested.

## Sync to Postgres

`sync` keeps a PostgreSQL database level with every session instead of writing an archive: the first cycle backfills,
later ones read what changed. It needs an OAuth credential from `pair` and the connection string in
`SESSION_SYNC_DATABASE_URL`. Schema, cycle semantics and the `json`/`jsonb` choice: [docs/sync.md](docs/sync.md).
`serve` adds a login-protected UI with separate `/sessions` and `/sync` pages for browsing synced sessions and
managing pairing/status, for a deployment where nothing can listen on the loopback port ([docs/serve.md](docs/serve.md)).
The sync follows recently active sessions over the server's event streams between cycles ([docs/sync.md](docs/sync.md)
§ Live following). The browser follows committed mirror changes across web replicas. In the cluster:
[docs/deploy.md](docs/deploy.md).

## Archive

```text
index.jsonl                  one line per session: the list item as sent (title, status, repo and branch, timestamps)
manifest.jsonl               one line per export: event_count, newest_sequence_num, gz_bytes
events/<session_id>.jsonl.gz one event per line, ordered by sequence_num, exactly as the API sent it
```

Files use `session_<x>` names; the API returns the same id as `cse_<x>`.

- Complete from `sequence_num` 1, including everything before a compaction (`compact_boundary` events mark them).
- Subagent traffic is included, marked by `payload.parent_tool_use_id`.
- Reasoning is mostly absent: the API stores a signature, not the text, for most `thinking` blocks.
- Transcripts contain whatever the session saw, including secrets in tool output and, if you ever pasted it into a
  session, your cookie. Treat the archive as sensitive.
