# HA-controller failover canary

Node-loss failover for `piraeus-ssd-2-autodiskful` with the Piraeus HA
controller enabled. [Fixtures](fixtures.yaml); [results](results.md). Apply
explicitly; Flux does not own these disposable objects.

The writer appends `seq timestamp hash` to `/data/log`, `sync`s, then prints
`ack <line>`. A restarted writer continues from the file's last sequence number,
so a sequence gap or a bad hash means lost or corrupted acknowledged data.

## Sequence

1. Apply `fixtures.yaml`; confirm the writer runs on the target node and LINSTOR
   shows two UpToDate copies plus a tiebreaker, `InUse` on the target.
2. Confirm the `ha-controller` DaemonSet is Ready and the controller properties
   `on-no-quorum=suspend-io` and `on-suspended-primary-outdated=force-secondary`.
3. Hard-reset the target with a one-shot privileged Pod in its own `privileged`
   namespace running `echo b > /proc/sysrq-trigger` (no sync, no shutdown).
   **Gotcha:** the Pod stays listed on the node after the reboot; delete its
   namespace immediately so kubelet cannot start it again.
4. Read the canary namespace events and the log's gap; check every hash.
5. After the node returns, confirm the `drbd.linbit.com/lost-quorum` taint is
   removed and LINSTOR has no faulty resources.
6. Delete the namespace, the retained PV and the LINSTOR resource definition.
