# NFS and CephFS mmap Support vs virtiofs

**TL;DR**:

- ✅ **NFS**: Full MAP_SHARED mmap support, would work fine
- ✅ **CephFS (kernel client)**: Full MAP_SHARED mmap support, would work fine
- ❌ **CephFS (ceph-fuse)**: Same limitations as virtiofs (FUSE-based)

## Technical Analysis

### NFS Implementation

**File**: `linux/fs/nfs/file.c`

```c
nfs_file_mmap_prepare(struct vm_area_desc *desc)
{
    // ...
    status = generic_file_mmap_prepare(desc);
    if (!status) {
        desc->vm_ops = &nfs_file_vm_ops;
        status = nfs_revalidate_mapping(inode, file->f_mapping);
    }
    return status;
}
```

**Key points**:

- Uses `generic_file_mmap_prepare()` - standard kernel implementation
- No MAP_SHARED restrictions
- No ENODEV returns
- Full POSIX mmap semantics

**Why it works**:

- NFS is a **native kernel filesystem driver** (not FUSE-based)
- Cache coherency built into NFS protocol (client-server coordination)
- Decades of maturity and optimization
- Used everywhere in production (enterprises, datacenters)

### CephFS Kernel Client Implementation

**File**: `linux/fs/ceph/addr.c`

```c
int ceph_mmap_prepare(struct vm_area_desc *desc)
{
    struct address_space *mapping = desc->file->f_mapping;

    if (!mapping->a_ops->read_folio)
        return -ENOEXEC;
    desc->vm_ops = &ceph_vmops;
    return 0;
}
```

**Key points**:

- Simple implementation, no restrictions
- No MAP_SHARED checks
- No ENODEV returns
- Uses standard page cache mechanisms

**Why it works**:

- CephFS kernel client is a **native kernel filesystem driver**
- Distributed cache coherency handled by Ceph protocol (MDS + OSD)
- Recommended deployment method for CephFS

### CephFS FUSE Client (ceph-fuse)

**Mount method**: `ceph-fuse /mnt/ceph`

**Implementation**: User-space FUSE daemon

**Result**: ❌ **Same limitations as virtiofs**

- FUSE-based, not native kernel driver
- Subject to FUSE's MAP_SHARED restrictions
- Would hit the same ENODEV error

### virtiofs (for comparison)

**File**: `linux/fs/fuse/file.c`

```c
if ((vma->vm_flags & VM_MAYSHARE) && !fc->direct_io_allow_mmap)
    return -ENODEV;  // ← Explicit rejection
```

**Why it doesn't work**:

- FUSE-based (userspace daemon over virtio)
- Cannot guarantee cache coherency for MAP_SHARED
- Requires explicit server support (FUSE_DIRECT_IO_ALLOW_MMAP flag)

## Verification Commands

### Check NFS

```bash
# Mount NFS
sudo mount -t nfs server:/export /mnt/nfs

# Test MAP_SHARED (should work)
cd /code/github.com/libgit2/libgit2
gcc -o test_mmap test_mmap.c
./test_mmap /mnt/nfs/test.git/packed-refs
# Should succeed
```

### Check CephFS Kernel Client

```bash
# Mount with kernel client
sudo mount -t ceph mon1,mon2,mon3:/ /mnt/ceph -o name=admin,secret=XXX

# Test MAP_SHARED (should work)
./test_mmap /mnt/ceph/test.git/packed-refs
# Should succeed
```

### Check CephFS FUSE Client

```bash
# Mount with FUSE
ceph-fuse -m mon1,mon2,mon3 /mnt/ceph-fuse

# Test MAP_SHARED (will likely fail like virtiofs)
./test_mmap /mnt/ceph-fuse/test.git/packed-refs
# Likely fails with ENODEV
```

## Comparison Table

| Filesystem          | Type          | MAP_SHARED | devenv/Nix | Notes                    |
| ------------------- | ------------- | ---------- | ---------- | ------------------------ |
| **NFS**             | Native kernel | ✅ Yes     | ✅ Works   | Production-ready, mature |
| **CephFS (kernel)** | Native kernel | ✅ Yes     | ✅ Works   | Recommended CephFS mount |
| **CephFS (fuse)**   | FUSE          | ❌ No      | ❌ Fails   | Not recommended for dev  |
| **virtiofs**        | FUSE          | ❌ No      | ❌ Fails   | VM shared folders only   |
| **ext4/btrfs/zfs**  | Native kernel | ✅ Yes     | ✅ Works   | Local filesystems        |

## Performance Considerations

### NFS

- **Latency**: Network round-trips for metadata operations
- **Throughput**: Good for sequential, worse for random
- **Caching**: Client-side attribute and data caching with timeout-based invalidation
- **Best for**: General-purpose network storage, established infrastructure
- **Typical speeds**: 100-1000 MB/s depending on network and server

### CephFS (kernel client)

- **Latency**: Slightly higher than NFS due to distributed architecture
- **Throughput**: Excellent for large files, scales with cluster
- **Caching**: MDS tracks client capabilities for coherent caching
- **Best for**: Large-scale storage, parallel workloads, multi-client access
- **Typical speeds**: 100-10000 MB/s depending on cluster size

### virtiofs

- **Latency**: Very low (shared memory, no network)
- **Throughput**: Very high (direct host filesystem access)
- **Caching**: Host kernel cache + guest kernel cache (DAX mode available)
- **Best for**: VM-host file sharing when mmap not critical
- **Typical speeds**: 1000-5000+ MB/s

## Recommendations

### For your use case (/code development)

**Best option: NFS**

- Mature, well-tested
- Works with all Linux tools
- Easy to set up
- Acceptable performance for code/Git operations

**Setup**:

```bash
# On host (assuming Linux)
sudo apt install nfs-kernel-server
echo "/code *(rw,sync,no_subtree_check,no_root_squash)" | sudo tee -a /etc/exports
sudo exportfs -ra

# In VM
sudo mount -t nfs host-ip:/code /code
```

**Alternative: CephFS kernel client**

- Better if you already have Ceph infrastructure
- More complex setup
- Overkill for single-user dev environment

### Migration strategy

1. **Test with NFS first**:

   ```bash
   # Mount NFS to a test location
   sudo mount -t nfs host:/code /mnt/test-nfs
   cd /mnt/test-nfs/gitlab.com/agentydragon/ducktape/adgn
   devenv shell  # Should work!
   ```

2. **If successful, remount /code**:

   ```bash
   sudo umount /code
   sudo mount -t nfs host:/code /code
   # Update /etc/fstab for persistence
   ```

3. **Performance tuning** (if needed):
   ```bash
   # Mount with performance options
   sudo mount -t nfs host:/code /code \
     -o vers=4.2,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2
   ```

## Known Issues

### NFS

- File locking can be flaky (use `nolock` mount option if issues arise)
- User ID mapping (use `no_root_squash` on server for dev environments)
- Time sync important (use NTP on both host and guest)

### CephFS

- Requires Ceph cluster (non-trivial setup)
- MDS can be bottleneck for metadata-heavy workloads
- Kernel client requires matching kernel and Ceph versions

### General Network Filesystem Caveats

- Power loss can cause stale file handles
- Network interruptions cause I/O hangs (use `soft` mount option carefully)
- Attribute caching can cause stale metadata (tune `actimeo` if needed)

## Conclusion

**Yes, both NFS and CephFS kernel client would work with devenv**, unlike virtiofs.

The fundamental difference:

- **NFS/CephFS kernel**: Native kernel implementations with full mmap support
- **virtiofs/ceph-fuse**: FUSE-based with MAP_SHARED restrictions

For your specific use case (dev environment with `/code` repository), **NFS is the simplest solution that will definitely work**.
