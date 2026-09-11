# Agentplane Git index

A standalone, single-collection semantic search service. It polls one Flux `GitRepository`,
downloads its published tar.gz artifact, verifies the SHA-256 digest, and incrementally
publishes searchable file versions. No Agentplane app or Haku Console service is required.

Only strict UTF-8 file contents are embedded. NUL-containing text is also excluded because
PostgreSQL text columns cannot store it. Excluded files remain in snapshot membership with
zero chunks, so a text-to-binary change removes the old searchable version. There is no
extension-based filter or encoding detection/conversion.

The container is `git.allegedly.works/ducktape-ci/agentplane-index`, published by the image
roster after its test gate passes. Use a pinned published tag. The Bazel binary is
`//x/agentplane/indexing:main_bin`; the container target is `:image`.

## Configuration

Flags use kebab case; environment variables have prefix `AGENTPLANE_INDEX_`.

| Required variable                 | Meaning                                                                          |
| --------------------------------- | -------------------------------------------------------------------------------- |
| `DATABASE_URL`                    | Dedicated `postgresql+asyncpg://` database URL                                   |
| `SOURCE_NAMESPACE`, `SOURCE_NAME` | Flux `GitRepository` to follow                                                   |
| `EMBEDDING_URL`                   | OpenAI-compatible base URL, including `/v1`                                      |
| `EMBEDDING_MODEL`                 | Exact model identity returned by the embedding endpoint                          |
| `EMBEDDING_API_KEY`               | Provider credential; a placeholder is sufficient for an unauthenticated provider |
| `READ_TOKEN`                      | Nonempty bearer credential for search and status                                 |

Optional settings include `QUERY_INSTRUCTION`, `POLL_SECONDS` (30),
`EMBEDDING_TIMEOUT_SECONDS` (60), `GC_SECONDS` (3600), `GC_GRACE_SECONDS` (86400),
`HOST` (`0.0.0.0`), `PORT` (8080), and `KUBECONFIG` (omit for in-cluster credentials).
`CHUNK_BUDGET` and `ARCHIVE_LIMITS` accept JSON objects; archive limits must be positive integers.
The default archive limits are 128 MiB compressed, 512 MiB expanded, 8 MiB per file,
and 100,000 entries. Exceeding a limit rejects the whole snapshot; use Flux ignore rules
or explicitly raise limits for larger sources. Archive contents are held in memory during
validation, so size the pod for expanded input plus parsing and database buffers.

Provide PostgreSQL with the pgvector extension available. Startup creates the extension and
the service-owned `agentplane_index` schema, so the initial database role needs those
privileges. Existing Haku tables and migrations are not used. This initial schema has no
upgrade migrations; incompatible future changes must ship an explicit migration or require
a separate database. Startup rejects a changed model or chunking configuration.

The worker needs Kubernetes `get` on the named resource, HTTP access to source-controller's
artifact endpoint, and access to PostgreSQL and the embedding endpoint. It never needs Git
credentials. A Role in the source namespace can restrict its rule to:

```yaml
apiGroups: [source.toolkit.fluxcd.io]
resources: [gitrepositories]
resourceNames: [your-source]
verbs: [get]
```

Bind that Role to the worker's ServiceAccount, even if the worker runs in another namespace.
Polling needs neither `list` nor `watch`. Deploy one replica initially; database advisory
locks also serialize mutations from overlapping replicas during a rollout. Give the pod a
Service on port 8080 with readiness/liveness probes at `/healthz`. Mount secrets through
`secretKeyRef` or `envFrom`; configure `imagePullSecrets` for the Forgejo registry. A typical
Deployment container configuration is:

```yaml
name: index
image: git.allegedly.works/ducktape-ci/agentplane-index:<published-tag>
envFrom:
  - secretRef:
      name: agentplane-index
env:
  - name: AGENTPLANE_INDEX_SOURCE_NAMESPACE
    value: flux-system
  - name: AGENTPLANE_INDEX_SOURCE_NAME
    value: your-source
ports:
  - containerPort: 8080
readinessProbe:
  httpGet: { path: /healthz, port: 8080 }
livenessProbe:
  httpGet: { path: /healthz, port: 8080 }
```

Choose the source and dedicated database when deploying. Place the deployment behind the
intended private service boundary and use TLS for
bearer-authenticated requests outside a trusted internal network.

## Read API

`GET /status` and `POST /search` require `Authorization: Bearer <READ_TOKEN>`.
Search accepts `{"query":"how are approvals handled?","limit":10}` (limit 1–100).
Responses contain `hits`, `status`, and an optional `warning`. Hits carry repository URL,
revision, artifact digest, path, byte range, exact embedded text, and cosine similarity.
The status contains desired/completed revisions, pending/served file counts, and separate
source, embedding, and garbage-collection error classes. Error details are in worker logs.
`GET /healthz` checks database access; source/provider outages appear in status rather than
forcing pod restarts. Search still requires the embedding endpoint to embed the query.

## Storage and updates

`snapshots` and `snapshot_files` describe accepted manifests. `state` holds desired and
completed snapshot pointers and the pinned configuration. `served_files` is the per-path
publication pointer. Blobs, chunk layouts, exact content, and model-specific embeddings
are cached separately. Snapshot identity includes artifact digest, revision, and repository
URL: two revisions can contain identical bytes while needing different citations.

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
authentication, and the artifact-to-search path. Source tests cover Flux readiness, digest
verification, archive limits, and unsafe entries. Flux's artifact contract is documented in
the [GitRepository reference](https://fluxcd.io/flux/components/source/gitrepositories/).
