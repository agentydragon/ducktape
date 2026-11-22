# Firecracker cleanup overlap and cold K3s image seeding, 2026-11-22

Keep serial Firecracker cleanup and the current cold K3s image import. The first pilot lengthened the phase it aimed to shorten. The second shortened stack setup but increased elapsed time on the worker. Both experiments used manual KVM targets. The linked PRs preserve the patches and reproduction details.

## Firecracker cleanup overlap

The pilot started the proxy-trust case as soon as the live assertions finished, while the live guest was still being cleaned up. Both cases kept separate Sandbox scopes bound to their UIDs. Both pytest cases passed in each arm and the persistence and Firecracker runtime receipts confirmed cleanup. [Pilot and commands](https://github.com/agentydragon/ducktape/pull/18985)

| Arm                                                                                  | Retained fixture | TestRunner process | End of live assertions through both cleanups | Proxy-trust case including cleanup |
| ------------------------------------------------------------------------------------ | ---------------- | -----------------: | -------------------------------------------: | ---------------------------------: |
| [Serial](https://app.buildbuddy.io/invocation/4b0606c7-8a0b-4063-8d81-8f8d3d59aa71)  | Cold             |            534.3 s |                                      108.7 s |                             65.4 s |
| [Overlap](https://app.buildbuddy.io/invocation/76562ec5-f377-4680-b552-3d2c0a159f39) | Warm             |            442.6 s |                                      133.1 s |                            133.1 s |

Proxy trust overlapped **68.3 s** of live cleanup, yet the interval from the end of live assertions through final cleanup grew by **24.4 s**. The shorter whole-process time came from a single comparison across different workers and fixture warmth. To revisit this result, compare repeated runs with matched fixture warmth and enough worker capacity for both cases.

## Cold K3s image seed

Both arms created fresh K3s clusters and ran the Claude and Codex VM cases. The candidate imported a digest-deduplicated archive of pinned KubeVirt and Kyverno layers and verified the expected images and CRI pins. Containerd still unpacked the layers. The archive added about 1.15 GB of input. A build-cache miss cost 42.54 s to pack and 4.70 s to upload. [Pilot and patch](https://github.com/agentydragon/ducktape/pull/18988)

| Final comparison | Worker input fetch | Stack setup | Worker start through test process end | Cases passed |
| ---------------- | -----------------: | ----------: | ------------------------------------: | -----------: |
| Control          |             0.73 s |    157.74 s |                              289.01 s |          2/2 |
| Seed             |             6.10 s |    156.93 s |                              293.98 s |          2/2 |

On the published pilot commit the seed saved **0.81 s** of stack setup and cost **4.97 s** from worker start through process end. This clock excludes scheduler queue time. The archive build is a separate cost. [Final uncached comparison](https://app.buildbuddy.io/invocation/7ad9dfe8-87b0-4e14-8aa5-22f582a16cd6)

The earlier passing control took 476.15 s on the worker. Both seeded runs passed and took 482.73 s and 628.39 s, with input fetches of 10.12 s and 73.16 s. A second control reached stack readiness, then both cases failed on HTTP 503 from the environment watch. Worker and input-cache residency varied. Revisit seeding if the archive can already be resident on eligible workers or if the import can also avoid unpacking. [Earlier comparison and test results](https://app.buildbuddy.io/invocation/8d181e06-b544-45eb-8ce8-3c26a6de7376)
