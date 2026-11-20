# Rugged suspend and resume evidence

These captures follow suspend and resume failures on Rugged. The [modem investigation](../../../nix/debug/rugged/hw/suspend_research/README.md) describes the longer-running mechanism work and its earlier capture tools.

- [October 23](20261023T192817_0700/findings.md) records the suspend sequence and initial instrumentation.
- [October 25](20261025T182859_0700/findings.md) records a focused MHI suspend trace.
- [October 27](20261027T084500_0700/findings.md) records additional suspend evidence and MBIM timeouts after resume.
- [November 8](20261108T191127_0700/findings.md) captures an MHI `-EBUSY` suspend failure and the upstream comparison.
