# virtiofsd Cache Policies

**Source**: virtiofsd v1.13.3 (`/code/gitlab.com/virtio-fs/virtiofsd`)

## Available Policies

| Policy       | CLI Flag           | Description                                        |
| ------------ | ------------------ | -------------------------------------------------- |
| **auto**     | `--cache=auto`     | Default. Close-to-open consistency with 1s timeout |
| **always**   | `--cache=always`   | Aggressive caching, 24hr timeout                   |
| **metadata** | `--cache=metadata` | Cache directories/attrs, direct I/O for files      |
| **never**    | `--cache=never`    | No caching, 0s timeout, direct I/O for everything  |

**Note**: `cache=none` is a legacy alias for `cache=never` (see `src/main.rs:446`).

## Policy Details

### cache=never

```rust
// From src/passthrough/mod.rs:96-99
/// The client should never cache file data and all I/O should be directly forwarded to the
/// server. This policy must be selected when file contents may change without the knowledge of
/// the FUSE client (i.e., the file system does not have exclusive access to the directory).
Never,
```

**Behavior**:

- Timeout: 0 seconds
- Readdirplus: Disabled
- File I/O: Direct I/O
- Directory I/O: Direct I/O

### cache=metadata

```rust
// From src/passthrough/mod.rs:101-105
/// This is almost same as Never, but it allows page cache of directories, dentries and attr
/// cache in guest. In other words, it acts like cache=never for normal files, and like
/// cache=always for directories, besides, metadata like dentries and attrs are kept as well.
/// This policy can be used if:
/// 1. the client wants to use Never policy but it's performance in I/O is not good enough
Metadata,
```

**Behavior**:

- Timeout: 86400 seconds (24 hours)
- Readdirplus: Enabled
- File I/O: Direct I/O
- Directory I/O: Cached with CACHE_DIR and KEEP_CACHE flags

### cache=auto

```rust
// From src/passthrough/mod.rs:108-114
/// The file system will use close-to-open consistency. This means that any cached contents of
/// the file are invalidated the next time that file is opened.
#[default]
Auto,
```

**Behavior**:

- Timeout: 1 second
- Readdirplus: Enabled
- File I/O: Close-to-open consistency

### cache=always

```rust
// From src/passthrough/mod.rs:116-119
/// The file system will always allow the FUSE client to cache file data. This option should
/// only be selected when the file system has exclusive access to the directory.
Always,
```

**Behavior**:

- Timeout: 86400 seconds (24 hours)
- Readdirplus: Enabled
- File I/O: KEEP_CACHE flag set

## Timeout Configuration (from src/main.rs:705-710)

```rust
let timeout = match opt.cache {
    CachePolicy::Never => Duration::from_secs(0),
    CachePolicy::Metadata => Duration::from_secs(86400),
    CachePolicy::Auto => Duration::from_secs(1),
    CachePolicy::Always => Duration::from_secs(86400),
};
```

## Why cache=metadata Still Accumulates FDs

With `cache=metadata`:

1. **Directories cached for 24 hours** - each accessed directory creates an FD
2. **Dentries (directory entries) cached** - file names within directories
3. **Attr cache retained** - file metadata (stat info)
4. Only **file contents** use direct I/O

For a large codebase with development tools constantly scanning directories (git, IDE indexers, language servers), this creates hundreds of thousands of cached entries.

## Recommendation for Memory-Constrained Systems

Use `cache=never` if:

- Memory leaks from virtiofsd are causing problems
- The filesystem is shared (not exclusive access)
- Performance impact is acceptable (disk latency on every access)

Use `cache=metadata` if:

- Need better directory traversal performance
- Can tolerate memory growth over time
- Have swap or memory buffer to handle growth

Use NFS instead of virtiofs if:

- Need MAP_SHARED mmap support (devenv, Nix)
- Want predictable memory usage
- Can accept slightly higher latency (~50µs vs ~10µs)

## Proxmox Configuration

```bash
# Set cache policy (requires VM restart)
qm set <vmid> --virtiofs<N> <tag>,cache=never

# Example
qm set 100 --virtiofs1 code,cache=never

# Verify
qm config 100 | grep virtiofs
```
