# Gitea pull mirror MCP

This package provides a host-side FastMCP server that creates public Gitea pull
mirrors and starts asynchronous syncs. See [server.py](server.py) for its tool
behavior and [launcher.py](launcher.py) for its stdio entry point and options.

## Mirror workflow

1. For an existing mirror, call `get_repo_info(owner, repo)` and save its
   `mirror_updated` timestamp.
2. Call `trigger_mirror_sync(url)` with the upstream HTTPS repository URL. The
   server creates a public pull mirror if needed, starts a sync, and returns
   immediately with an empty response.
3. Poll `get_repo_info(owner, repo)` until `mirror_updated` changes.
4. Clone `{owner}/{repo}.git` from the mirror store after the timestamp changes.

`trigger_mirror_sync` uses the API token's Gitea user as owner and derives the
mirror name from the upstream host and path (for example,
`https://github.com/username/repo` becomes `github-com-username-repo`). A new
mirror is public; only use this server for upstream repositories intended to be
public in the Gitea instance. The token needs `write:repository` permission to
create and sync mirrors. `get_repo_info` only accepts repositories configured as mirrors;
the server recommends polling every 2-5 seconds (typical syncs take 5-60 seconds).

## Start the local Gitea instance

The compose file publishes Gitea at `http://localhost:3000` and stores its data
under `${HOME}/.combo_mcp/gitea` on the host:

```bash
docker compose -f x/gitea/docker-compose.yml up -d
```

The repository store is `${HOME}/.combo_mcp/gitea/git/repositories` on the host
(`/data/git/repositories` in the container). The service container is named
`public-git-gitea`. The init service writes an admin token to
`${HOME}/.combo_mcp/gitea/admin.token` with `repo,write:repository` scope. Set
the Gitea admin bootstrap values with `GITEA_ADMIN_USERNAME`,
`GITEA_ADMIN_PASSWORD`, and `GITEA_ADMIN_EMAIL` before startup. The default admin
password is `changeme`. Compose uses SQLite at `/data/gitea.db` and disables
Gitea registration and SSH.

Docker Desktop and Colima may report a different host-side bind path. Inspect
the `/data` mount when you need the resolved path:

```bash
docker inspect public-git-gitea --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}'
```

The command reports the host-side `/data` root; append `/git/repositories` for
the mirror store.

## Run the mirror server

Run the host-side stdio MCP server with the local URL and token file:

```bash
GITEA_BASE_URL=http://localhost:3000 \
  bb run //x/gitea/mcp_mirror:gitea_mirror_launcher -- \
  --token-file "$HOME/.combo_mcp/gitea/admin.token"
```

The launcher also accepts `--base-url` and `--token`; its environment options
are `GITEA_BASE_URL`, `GITEA_TOKEN`, and `GITEA_TOKEN_FILE`.
The base URL must be reachable from the host process running this MCP server.

## Clone from the mirror store

The MCP server manages mirrors through Gitea's API; a separate container runner
must execute Git. Give that runner an image with `git`, disable its network
(`--network none` in Docker), and mount the host repository store read-only
(`:ro`). Keep the Gitea token with the host MCP process. The clone container only
needs read access to its local mirror path:

```bash
git clone --reference /mnt/git-bare/agentydragon/github-com-username-repo.git \
  file:///mnt/git-bare/agentydragon/github-com-username-repo.git /workspace/repos/repo
```

Mount `${HOME}/.combo_mcp/gitea/git/repositories` at `/mnt/git-bare` or update the
clone paths to match the mount. This package provides the Gitea API manager; it
does not launch the container runner.
