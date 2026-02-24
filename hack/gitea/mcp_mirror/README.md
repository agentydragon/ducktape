# Gitea mirror + Docker exec integration

This document explains how to run a local Gitea instance, prepare pull mirrors,
and expose them to the Docker exec MCP server as a read-only volume. Clients can
then compose the `gitea_mirror` and `docker_exec` MCP servers to keep mirrors up
to date and clone them inside sandboxed containers.

## Overview

```
┌─────────────────┐  1. get_repo_info                ┌────────────────────┐
│ gitea_mirror MCP │ ◀───────────────────────────────  │ Gitea (pull mirrors)│
│                 │  2. trigger_mirror_sync          │                    │
│                 │ ───────────────────────────────▶ │                    │
│                 │  3. get_repo_info (poll)         │                    │
│                 │ ◀───────────────────────────────  │                    │
└─────────────────┘                                   └────────────────────┘
          ▲                                                     │
          │ owner/repo                                          │ host bind mount
          │                                                     ▼
┌─────────────────┐  git clone via exec tool        ┌────────────────────┐
│ docker_exec MCP  │ ─────────────────────────────▶ │ /mnt/git-bare/<repo>│
└─────────────────┘                                 └────────────────────┘
```

1. `gitea_mirror` MCP is run on the host with access to the Gitea API.
2. `docker_exec` MCP runs sandboxed containers with a read-only bind mount of
   the mirror store (e.g. `/Users/<user>/.combo_mcp/gitea/git/repositories`).
3. Agents call `get_repo_info` to get the initial repository state (GET `/repos/{owner}/{repo}`).
4. Agents call `trigger_mirror_sync` with an HTTPS repository URL. The tool creates
   a pull mirror (POST `/repos/migrate`), triggers an async sync (POST
   `/repos/{owner}/{repo}/mirror-sync`), and returns immediately (empty response,
   matching Gitea API).
5. Agents poll `get_repo_info` until the `mirror_updated` timestamp changes,
   indicating the sync is complete.
6. Once synced, agents construct the mirror path as `{owner}/{repo}.git` and clone
   from the read-only bind mount using `git clone --reference` for fast object reuse.

## Running Gitea locally

A minimal docker-compose file is available at
`src/adgn_llm/mcp/gitea/docker-compose.yml`:

```bash
docker compose -f src/adgn_llm/mcp/gitea/docker-compose.yml up -d
open http://localhost:3000
```

Complete the initial setup, create an admin token, and record:

- Base URL, e.g. `http://localhost:3000`
- Access token with `write:repository` scope (required for migrate + sync API)
- Mirror storage path inside the volume (`/data/git/repositories` inside the
  container, bind-mounted on the host).

To discover the host path of the mirror store when using Docker Desktop or
Colima:

```bash
docker inspect adgn-gitea --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}'
```

Mount this path read-only into the Docker exec MCP containers.

## Launching the MCP servers

### Gitea mirror MCP

```bash
export GITEA_BASE_URL="http://localhost:3000"
export GITEA_TOKEN="<token>"
adgn-mcp-gitea-mirror
```

The server exposes two tools:

- `get_repo_info`: Returns full repository information from Gitea API (matches GET /repos/{owner}/{repo})
- `trigger_mirror_sync`: Ensures mirror exists, triggers async sync, returns empty (matches POST /repos/{owner}/{repo}/mirror-sync)

### Docker exec MCP

```bash
MIRROR_ROOT="$(docker inspect adgn-gitea \
  --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}')/git/repositories"

adgn-mcp-docker-exec \
  --image ghcr.io/agentydragon/public-git-runner:latest \
  --working-dir /workspace \
  --network-mode none \
  --volumes "$MIRROR_ROOT:/mnt/git-bare:ro"
```

Flags of note:

- `--image` must include `git` and other tools needed inside the container.
- `--network-mode none` keeps containers offline; change if API access is
  required.
- `--volumes` accepts comma-separated bind specs (`host:container[:mode]`). Use
  `:ro` to keep the mirror read-only inside the container.

## Client workflow

1. **Get initial state**: Call `get_repo_info` to get the initial mirror state:

   ```json
   {
     "owner": "agentydragon",
     "repo": "github-com-username-repo"
   }
   ```

   Response (full Gitea Repository object - showing key fields):

   ```json
   {
     "id": 42,
     "name": "github-com-username-repo",
     "full_name": "agentydragon/github-com-username-repo",
     "description": "Example repository",
     "mirror": true,
     "mirror_updated": "2024-01-15T10:30:00Z",
     "mirror_interval": "8h0m0s",
     "size": 1234,
     "default_branch": "main",
     "clone_url": "https://gitea.example.com/agentydragon/github-com-username-repo.git",
     "html_url": "https://gitea.example.com/agentydragon/github-com-username-repo",
     "stars_count": 0,
     "forks_count": 0,
     "created_at": "2024-01-15T10:00:00Z",
     "updated_at": "2024-01-15T10:30:00Z",
     ...
   }
   ```

   Save the `mirror_updated` timestamp.

2. **Trigger the sync**: Call `trigger_mirror_sync` with the upstream URL:

   ```json
   {
     "url": "https://github.com/username/repo"
   }
   ```

   Response: Empty (matching Gitea's mirror-sync endpoint)

   ```json
   {}
   ```

3. **Poll for completion**: Repeatedly call `get_repo_info` until `mirror_updated` changes:

   ```json
   {
     "owner": "agentydragon",
     "repo": "github-com-username-repo"
   }
   ```

   When `mirror_updated` differs from the initial timestamp, the sync is complete.

4. **Clone from mirror**: Construct mirror path as `{owner}/{repo}.git` and use the `exec` tool:

   ```json
   {
     "cmd": [
       "sh",
       "-lc",
       "mkdir -p /workspace/repos && git clone --reference /mnt/git-bare/agentydragon/github-com-username-repo.git file:///mnt/git-bare/agentydragon/github-com-username-repo.git /workspace/repos/repo"
     ]
   }
   ```

5. **Work with the repo**: Subsequent `exec` calls can operate inside the checkout
   (e.g. run tests or edit files under `/workspace/repos/repo`).
