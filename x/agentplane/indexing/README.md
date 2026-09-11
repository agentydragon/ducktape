# Agentplane Git index

A standalone, single-collection semantic search service. It keeps a bare clone of one
branch of one Git remote, fetches it on every poll, and incrementally publishes
searchable file versions from the tree of the branch tip. No Agentplane app, Haku Console
service, or Flux source is required.

Only strict UTF-8 file contents are embedded. NUL-containing text is also excluded because
PostgreSQL text columns cannot store it. Excluded files remain in snapshot membership with
zero chunks, so a text-to-binary change removes the old searchable version. There is no
extension-based filter or encoding detection/conversion; `IGNORE` patterns are the only
way to leave a path out of a snapshot.

The container is `git.allegedly.works/ducktape-ci/agentplane-index`, published by the image
roster after its test gate passes. Use a pinned published tag. The Bazel binary is
`//x/agentplane/indexing:main_bin`; the container target is `:image`.

## Configuration

Flags use kebab case; environment variables have prefix `AGENTPLANE_INDEX_`.

| Required variable   | Meaning                                                                          |
| ------------------- | -------------------------------------------------------------------------------- |
| `DATABASE_URL`      | Dedicated `postgresql+asyncpg://` database URL                                   |
| `REPOSITORY_URL`    | Git remote: an HTTP(S) URL or a local path                                       |
| `BRANCH`            | Branch whose tip is indexed                                                      |
| `CHECKOUT_DIR`      | Directory for the bare clone; created on first use, re-cloned if it is empty     |
| `EMBEDDING_URL`     | OpenAI-compatible base URL, including `/v1`                                      |
| `EMBEDDING_MODEL`   | Exact model identity returned by the embedding endpoint                          |
| `EMBEDDING_API_KEY` | Provider credential; a placeholder is sufficient for an unauthenticated provider |
| `READ_TOKEN`        | Nonempty bearer credential for search and status                                 |

Optional settings include `GIT_USERNAME` + `GIT_PASSWORD` (set together; HTTP basic auth
for the remote), `GIT_CA_BUNDLE` (PEM bundle for HTTPS remotes — libgit2 does not read
`SSL_CERT_FILE`), `IGNORE` (gitignore-syntax patterns, one per line; an ignored directory
is never descended), `QUERY_INSTRUCTION`, `POLL_SECONDS` (30),
`EMBEDDING_TIMEOUT_SECONDS` (60), `GC_SECONDS` (3600), `GC_GRACE_SECONDS` (86400),
`HOST` (`0.0.0.0`), and `PORT` (8080). `CHUNK_BUDGET` and `SNAPSHOT_LIMITS` accept JSON
objects; snapshot limits must be positive integers. The default snapshot limits are 512 MiB
in total, 8 MiB per file, and 100,000 files. Exceeding a limit rejects the whole snapshot;
use `IGNORE` or explicitly raise limits for larger sources. Snapshot contents are held in
memory during ingestion, so size the pod for the tree plus parsing and database buffers.

Provide PostgreSQL with the pgvector extension available. Startup creates the extension and
the service-owned `agentplane_index` schema, so the initial database role needs those
privileges. Existing Haku tables and migrations are not used. This initial schema has no
upgrade migrations; incompatible future changes must ship an explicit migration or require
a separate database. Startup rejects a changed model or chunking configuration.

The worker needs network access to the Git remote, PostgreSQL, and the embedding endpoint,
and a writable `CHECKOUT_DIR` sized for the clone. It needs no Kubernetes API access. A
private remote gets `GIT_USERNAME`/`GIT_PASSWORD` from a Secret; a public one needs nothing.
Deploy one replica initially; database advisory locks also serialize mutations from
overlapping replicas during a rollout. Give the pod a Service on port 8080 with
readiness/liveness probes at `/healthz`. Mount secrets through `secretKeyRef` or `envFrom`;
configure `imagePullSecrets` for the Forgejo registry. A typical Deployment container
configuration is:

```yaml
name: index
image: git.allegedly.works/ducktape-ci/agentplane-index:<published-tag>
envFrom:
  - secretRef:
      name: agentplane-index
env:
  - name: AGENTPLANE_INDEX_REPOSITORY_URL
    value: https://git.example/owner/repository.git
  - name: AGENTPLANE_INDEX_BRANCH
    value: main
  - name: AGENTPLANE_INDEX_CHECKOUT_DIR
    value: /var/lib/agentplane-index/repository
  - name: AGENTPLANE_INDEX_IGNORE
    value: |
      generated/
      *.gz
volumeMounts:
  - name: repository
    mountPath: /var/lib/agentplane-index
ports:
  - containerPort: 8080
readinessProbe:
  httpGet: { path: /healthz, port: 8080 }
livenessProbe:
  httpGet: { path: /healthz, port: 8080 }
```

Choose the remote and dedicated database when deploying. Place the deployment behind the
intended private service boundary and use TLS for
bearer-authenticated requests outside a trusted internal network.

## Read API

`GET /status` and `POST /search` require `Authorization: Bearer <READ_TOKEN>`.
Search accepts `{"query":"how are approvals handled?","limit":10}` (limit 1–100).
Responses contain `hits`, `status`, and an optional `warning`. Hits carry repository URL,
revision, tree id, path, byte range, exact embedded text, and cosine similarity.
The status contains desired/completed revisions, pending/served file counts, and separate
source, embedding, and garbage-collection error classes. Error details are in worker logs.
`GET /healthz` checks database access; remote/provider outages appear in status rather than
forcing pod restarts. Search still requires the embedding endpoint to embed the query.

## Storage and updates

`snapshots` and `snapshot_files` describe accepted manifests. `state` holds desired and
completed snapshot pointers and the pinned configuration. `served_files` is the per-path
publication pointer. Blobs, chunk layouts, exact content, and model-specific embeddings
are cached separately. Snapshot identity includes tree id, revision, and repository
URL: two revisions can contain identical trees while needing different citations.

The source loop accepts a validated manifest atomically, removes deleted paths, and advances
unchanged paths' provenance immediately. The embedding loop processes pending files with
bounded embedding batches and publishes each file only when its complete layout is ready.
Reads use committed served membership throughout. Source ingestion, embedding batches, and
garbage collection share a transaction advisory lock; ordinary search does not acquire it.
The lock is held during an embedding batch's network request, so source acceptance can wait
up to the configured request timeout while search continues.

Pending files are processed in path order. A persistently rejected embedding input blocks
later pending files until the provider recovers or a newer source snapshot removes/changes
that input; status remains incomplete and previously served files remain available.

GC first marks unreachable snapshots, blobs, and content. Later sweeps delete eligible
objects in bounded object batches after the grace period. Each layer gets its own grace
period, so physical cleanup can take several periods. Desired and last-completed snapshots,
and snapshots supplying any served file, are roots; their manifests retain all referenced
blobs. PostgreSQL autovacuum reclaims deleted row space for reuse. No routine `VACUUM FULL`
is used. This cache is local to the database; separate deployments do not share vectors.

## Verification

```sh
bbr test //x/agentplane/indexing:all
```

The store/API tests use PostgreSQL with pgvector on RBE. They cover publication and provider
failure boundaries, revision supersession, embedding reuse, restart, garbage collection,
authentication, and the commit-to-search path. Source tests drive a throwaway upstream
repository through commits and cover incremental fetches, ignore patterns, entry kinds
(symlinks and submodules are skipped), snapshot limits, and a relocated remote.
