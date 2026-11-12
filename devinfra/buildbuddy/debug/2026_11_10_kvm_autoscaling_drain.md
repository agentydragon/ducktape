# KVM pool automatic scale-down drain

On November 11, 2026 UTC, a live test of the `kvm-vm` pool confirmed that an
automatic scale-down can select a busy VM and let its action finish before the VM
and its disposable disk are removed. This is one observed run, not a guarantee
for every action or for node failures. The pool's
[shutdown and scaling configuration](../../../cluster/k8s/buildbuddy-kvm-worker/README.md)
describes the bounded drain and its input-download limitation.

## Setup

The pool started with one ready worker. KEDA was configured for one to three VMs,
with a two-minute scale-up stabilization window and a 15-minute scale-down
stabilization window. Two uncached `bb execute` Docker actions targeted the
self-hosted `kvm-vm` pool. The first printed the guest's DMI UUID, slept for
2,100 seconds, and printed a completion marker. A second, 90-second action
queued behind it to trigger scale-up. Both used `EstimatedCPU=4` and the pinned
RBE container image; the occupied worker's assigned-CPU metric reported 250
milliCPU during the long action. The test used disposable work and did not
change the pool or autoscaler configuration.

## Observations

Times are UTC. Pool and metrics observations were sampled approximately every
15 seconds; the VM deletion timestamp and action times come from Kubernetes
and BuildBuddy execution metadata respectively.

| Time     | Observation                                                                                                                                                     |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 00:56:28 | Long action started on `buildbuddy-kvm-worker-1`, guest UUID `93f6123d-a630-40ba-99ac-75328b1653c7`.                                                            |
| 00:59:34 | First sample with the pool requesting two VMs.                                                                                                                  |
| 01:02:26 | First sample with two ready VMs. The short action started on worker 0 at 01:02:41 and exited successfully at 01:04:12.                                          |
| 01:05:02 | First sample with two ready VMs and demand one; the remaining action was on worker 1.                                                                           |
| 01:19:45 | Automatic scale-down selected worker 1. Its VM and VMI gained deletion timestamps, and KubeVirt recorded `Signaled Graceful Shutdown`. Worker 0 remained ready. |
| 01:31:28 | The long action printed its completion marker and exited 0 on the same guest UUID, 11 minutes 43 seconds after deletion began.                                  |

After the action finished, worker 1 and its VMI disappeared. Its `work-1` PVC,
PV, and LINSTOR resource definition were gone; the pool reported one desired
and one ready worker. This verifies the disposable-volume cleanup for this run
as well as a busy worker's automatic drain.

The demand signal still counts local queue reservations and can overcount
already-running work. [Issue #15568](https://github.com/agentydragon/ducktape/issues/15568)
tracks a distinct backlog signal; this drain test does not resolve that issue.
