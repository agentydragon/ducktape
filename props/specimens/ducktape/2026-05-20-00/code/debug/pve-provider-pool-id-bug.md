# bpg/terraform-provider-proxmox: `pool_id` update fails when VM is already a pool member

Provider version: v0.91.0 (commit `c459b3ed`)

## Summary

When a `proxmox_virtual_environment_vm` resource is updated to add a `pool_id`
attribute for a VM that is already a member of that pool (added manually via
the Proxmox UI, via `proxmox_virtual_environment_pool_membership`, or from a
previous apply), the provider unconditionally calls the Proxmox
`PUT /pools/{pool}` API to add the VM. The Proxmox API returns HTTP 500 because
the VM is already a member of the target pool.

## Error message

```
Error: while adding VM 110 to pool pool-agent-test: error updating pool:
received an HTTP 500 response - Reason: update pools failed:
VM 110 is already a pool member
```

## Scenario

1. VM 110 exists and is already a member of `pool-agent-test` (added manually
   or via `pool_membership`).
2. The Terraform/OpenTofu config for the VM resource does not have `pool_id`
   set, so the state has `pool_id = ""`.
3. User adds `pool_id = "pool-agent-test"` to the VM resource config.
4. `tofu plan` detects a change: `pool_id: "" -> "pool-agent-test"`.
5. `tofu apply` calls `vmUpdatePool`, which sees `oldPool = ""` and
   `newPool = "pool-agent-test"`.
6. Since `oldPool` is empty, the remove-from-old-pool step is skipped.
7. The provider calls `PUT /pools/pool-agent-test` with `vms=110` to add
   the VM to the pool.
8. Proxmox returns HTTP 500: the VM is already in that pool.

## Root cause analysis

The bug is in the `vmUpdatePool` function at:

**File:** `/code/github.com/bpg/terraform-provider-proxmox/proxmoxtf/resource/vm/vm.go`
**Lines:** 5332-5381

```go
// vmUpdatePool moves the VM to the pool it is supposed to be in if the pool ID changed.
// Only updates pool_id if it is explicitly set to a non-empty value to avoid conflicts with pool_membership resource.
func vmUpdatePool(
    ctx context.Context,
    d *schema.ResourceData,
    api *pools.Client,
    vmID int,
) error {
    if !d.HasChange(mkPoolID) {
        return nil
    }

    oldPoolValue, newPoolValue := d.GetChange(mkPoolID)
    oldPool := oldPoolValue.(string)
    newPool := newPoolValue.(string)

    if cmp.Equal(newPool, oldPool) {
        return nil
    }

    if newPool == "" && oldPool != "" {
        return nil
    }

    vmList := (types.CustomCommaSeparatedList)([]string{strconv.Itoa(vmID)})

    tflog.Debug(ctx, fmt.Sprintf("Moving VM %d from pool '%s' to pool '%s'", vmID, oldPool, newPool))

    if oldPool != "" {
        trueValue := types.CustomBool(true)
        poolUpdate := &pools.PoolUpdateRequestBody{
            VMs:    &vmList,
            Delete: &trueValue,
        }

        err := api.UpdatePool(ctx, oldPool, poolUpdate)
        if err != nil {
            return fmt.Errorf("while removing VM %d from pool %s: %w", vmID, oldPool, err)
        }
    }

    poolUpdate := &pools.PoolUpdateRequestBody{VMs: &vmList}

    err := api.UpdatePool(ctx, newPool, poolUpdate)       // <-- line 5375: unconditional add
    if err != nil {
        return fmt.Errorf("while adding VM %d to pool %s: %w", vmID, newPool, err)
    }

    return nil
}
```

The function detects a change in state (`pool_id: "" -> "pool-agent-test"`)
and proceeds to add the VM to the new pool. It never checks whether the VM is
**already** a member of the target pool before issuing the API call.

The underlying issue is a mismatch between Terraform state and Proxmox reality:

- The **Read** function (`vmReadCustom`, lines 4834-4848) discovers the VM's
  actual pool membership via `vmConfig.PoolID` or `findPoolForVM` and writes
  it to state.
- The **DiffSuppressFunc** (lines 1297-1299) suppresses diffs when
  `newVal == "" && oldVal != ""` -- i.e., when `pool_id` is removed from
  config but the state shows the VM is in a pool. This prevents the provider
  from trying to remove VMs from pools when `pool_id` is not set.
- However, the **opposite direction** is not handled: when `pool_id` is added
  to the config and the state shows `""` (because `pool_id` was never set in
  config before), but the VM is actually already in that pool on the Proxmox
  side.

The state can show `pool_id = ""` even though the VM is in a pool in two ways:

1. The VM was added to the pool via `proxmox_virtual_environment_pool_membership`
   and the DiffSuppressFunc suppressed the diff on the VM resource during the
   subsequent refresh.
2. The VM was added to the pool manually in the Proxmox UI, but the
   DiffSuppressFunc suppressed the diff because `pool_id` was not set in
   config.

Wait -- actually, re-reading the DiffSuppressFunc:

```go
DiffSuppressFunc: func(k, oldVal, newVal string, d *schema.ResourceData) bool {
    return newVal == "" && oldVal != ""
},
```

This suppresses diffs when `newVal` (config) is `""` and `oldVal` (state) is
non-empty. This means: if the config has no `pool_id` but the state learned
the VM is in a pool (from Read), the diff is suppressed, avoiding an unwanted
update.

But the Read function (line 4846) always sets `pool_id` in state to the
observed pool. So after a refresh, if the VM is in `pool-agent-test`, state
will have `pool_id = "pool-agent-test"`. When the user then adds
`pool_id = "pool-agent-test"` to their config, the diff should be
`oldVal = "pool-agent-test"` and `newVal = "pool-agent-test"` -- no change,
no update triggered.

This means the bug specifically manifests when:

1. The VM was added to the pool **after the last refresh** -- the state still
   has `pool_id = ""` because no refresh has run since the pool membership
   was created.
2. Or: the state was imported/created without `pool_id`, the VM was added to
   the pool externally, and the user runs `apply` (not `plan`/`refresh`)
   directly -- the update runs before the read can update the state.

In the Terraform SDK lifecycle, during `apply`, the update function runs
**before** the read function. So the sequence is:

1. Plan phase: reads state (`pool_id = ""`), reads config (`pool_id = "pool-agent-test"`), detects diff.
2. Apply phase: calls `vmUpdate` -> `vmUpdatePool` with `oldPool = ""`, `newPool = "pool-agent-test"`.
3. `vmUpdatePool` tries to add VM to pool -> HTTP 500.

The plan phase does call Read to refresh state, but only if the resource
already exists. The key issue is that the DiffSuppressFunc prevents the
state from being updated in some scenarios, or the state update from Read
hasn't propagated properly to the diff computation.

**The core problem is simple**: `vmUpdatePool` does not check whether the VM
is already in the target pool before calling the Proxmox API to add it.

## Suggested fix

Before adding the VM to the new pool, check if it is already a member. This
can be done by querying the pool and checking its members list, or by using
the `findPoolForVM` function that already exists in the codebase:

```go
func vmUpdatePool(
    ctx context.Context,
    d *schema.ResourceData,
    api *pools.Client,
    vmID int,
) error {
    if !d.HasChange(mkPoolID) {
        return nil
    }

    oldPoolValue, newPoolValue := d.GetChange(mkPoolID)
    oldPool := oldPoolValue.(string)
    newPool := newPoolValue.(string)

    if cmp.Equal(newPool, oldPool) {
        return nil
    }

    if newPool == "" && oldPool != "" {
        return nil
    }

    // Check if the VM is already in the target pool.
    currentPool, err := findPoolForVM(ctx, api, vmID)
    if err != nil {
        return fmt.Errorf("while checking current pool for VM %d: %w", vmID, err)
    }

    if currentPool == newPool {
        tflog.Debug(ctx, fmt.Sprintf("VM %d is already in pool '%s', skipping pool update", vmID, newPool))
        return nil
    }

    vmList := (types.CustomCommaSeparatedList)([]string{strconv.Itoa(vmID)})

    tflog.Debug(ctx, fmt.Sprintf("Moving VM %d from pool '%s' to pool '%s'", vmID, oldPool, newPool))

    // Remove from old pool (use currentPool instead of oldPool for accuracy)
    if currentPool != "" {
        trueValue := types.CustomBool(true)
        poolUpdate := &pools.PoolUpdateRequestBody{
            VMs:    &vmList,
            Delete: &trueValue,
        }

        err := api.UpdatePool(ctx, currentPool, poolUpdate)
        if err != nil {
            return fmt.Errorf("while removing VM %d from pool %s: %w", vmID, currentPool, err)
        }
    }

    poolUpdate := &pools.PoolUpdateRequestBody{VMs: &vmList}

    err = api.UpdatePool(ctx, newPool, poolUpdate)
    if err != nil {
        return fmt.Errorf("while adding VM %d to pool %s: %w", vmID, newPool, err)
    }

    return nil
}
```

An alternative, lighter fix is to catch the "already a pool member" error and
treat it as a no-op:

```go
err := api.UpdatePool(ctx, newPool, poolUpdate)
if err != nil {
    if strings.Contains(err.Error(), "already a pool member") {
        tflog.Debug(ctx, fmt.Sprintf("VM %d is already in pool %s, ignoring", vmID, newPool))
        return nil
    }
    return fmt.Errorf("while adding VM %d to pool %s: %w", vmID, newPool, err)
}
```

The first approach (checking membership before the API call) is more robust
because it also fixes a related issue: if the Terraform state has `oldPool`
as `""` but the VM is actually in a different pool, the current code skips
the removal step and tries to add the VM to the new pool. This would fail
with a different Proxmox error ("already a pool member" for the other pool).
By checking `currentPool` from the API, the function uses the real state
rather than possibly-stale Terraform state.

## Workaround

Add `lifecycle { ignore_changes = [pool_id] }` to the VM resource and manage
pool membership exclusively via the `proxmox_virtual_environment_pool_membership`
resource:

```hcl
resource "proxmox_virtual_environment_vm" "example" {
  node_name = "pve"
  name      = "my-vm"
  # Do not set pool_id here

  lifecycle {
    ignore_changes = [pool_id]
  }
}

resource "proxmox_virtual_environment_pool_membership" "example" {
  pool_id = proxmox_virtual_environment_pool.my_pool.pool_id
  vm_id   = proxmox_virtual_environment_vm.example.id
}
```

This prevents the VM resource from ever trying to manage pool membership,
delegating it entirely to the `pool_membership` resource which handles
the lifecycle more correctly (using `AllowMove` and `RequiresReplace`
semantics).
