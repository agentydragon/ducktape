# Epistemic State: GLM-5.2 on wyrm2 via Colibri

## Last updated: 2026-07-14T00:35:00-07:00

## 1. Objective

Determine whether `wyrm2` can serve GLM-5.2 experimentally through Colibri's
disk-streamed MoE runtime at a useful enough speed to expose behind cluster LiteLLM.
Start outside Kubernetes. Stage the 383.8 GB preconverted checkpoint on roomy HDD
storage while the cheap hardware and I/O probes finish, then move it to dedicated SSD
storage for the decisive full-model benchmark.

## 2. Available Action Space

- Run read-only hardware, filesystem, and LVM inspection over SSH.
- Build and run Colibri's tiny architecture oracle and CUDA fixture on `wyrm2`.
- Run Colibri's 19 MB random-read benchmark against candidate local SSD storage.
- If the probes pass, use the upstream-recommended preconverted Colibri INT4
  checkpoint rather than downloading and locally converting the much larger FP8 model.
- Later, expose `coli serve` through LiteLLM as a distinct local model.

## 3. Uncertainty Register

| ID  | Quantity                                                        | Prior                            | Source                                                        | Status   |
| --- | --------------------------------------------------------------- | -------------------------------- | ------------------------------------------------------------- | -------- |
| U1  | Colibri builds and its CUDA backend works on `wyrm2`            | Yes                              | CUDA backend correctness passed on both RTX 5090s             | Resolved |
| U2  | Candidate SSD sustains Colibri-shaped random reads              | Yes for `/games`-class local ZFS | Upstream `iobench` measured 5.95 GB/s                         | Resolved |
| U3  | Enough SSD capacity is safely available for the converted model | Yes                              | Dedicated 500 GB local-ZFS disk mounted at `/var/lib/colibri` | Resolved |
| U4  | Warm decode throughput is useful                                | 0.5-1.5 tok/s [VIBE]             | Nearest upstream community systems                            | Open     |
| U5  | Current text-only OpenAI API is sufficient for an experiment    | Yes                              | User accepts an experiment                                    | Resolved |

## 4. Hypothesis Space

| ID      | Hypothesis                                                            | Probability | Distinguishing test                                 |
| ------- | --------------------------------------------------------------------- | ----------- | --------------------------------------------------- |
| H1      | Host experiment works at roughly interactive-but-slow speed           | 0.55        | Full-model fixed-prompt 32-token decode             |
| H2      | It works but remains below 0.5 tok/s because storage or CPU dominates | 0.30        | Full-model runtime profile                          |
| H3      | Upstream model/runtime correctness mismatch blocks coherent output    | 0.08        | Full-model fixed-prompt coherence and repeatability |
| H4      | Dedicated SSD provisioning blocks or materially delays the experiment | 0.02        | Add a safely owned 450+ GB host disk                |
| H_other | Other failure                                                         | 0.05        | Staged probes and logs                              |

## 5. Evidence Log

### E1: Live host inventory

- Action: read-only SSH inspection of GPUs, CPU, RAM, filesystems, and LVM.
- Result: 2 RTX 5090 GPUs with 32,607 MiB each; Ryzen 9 9950X3D with 32 visible
  physical cores and AVX-512/VNNI; 94 GiB RAM total and 70 GiB currently available.
- Storage: `/games` has 354 GiB free on a 500 GB SSD virtual disk;
  `/var/local-path-provisioner` has 448 GiB free on a 500 GB SSD virtual disk;
  `openebs-proxmox-ssd` has 469.93 GiB unallocated VG extents. The existing Ollama
  model PVC is on the HDD-backed OpenEBS VG.
- Update: U1 and U3 more likely; U2 remains load-bearing and unmeasured.

### E2: Upstream Colibri constraints

- Source: <https://github.com/JustVugg/colibri>, accessed 2026-07-13.
- Result: approximately 370 GB INT4 checkpoint; approximately 11.4 GB cold expert
  reads per decoded token; Linux CUDA tier supports multiple RTX 5090s without a
  P2P/NCCL dependency; server implements text-only OpenAI chat completions.
- Update: the architecture is compatible in principle, but disk throughput and hot
  expert placement determine usefulness.

### E3: Tiny CPU oracle

- Action: generated the current upstream tiny GLM oracle and ran the native AVX-512
  engine.
- Result: 25/32 token positions matched, with 7 mismatches. Native speed was
  239.2 positions/s. The generated oracle also differs from the reference committed
  in the current Colibri tree, so this is an upstream harness/reproducibility warning,
  not evidence of a `wyrm2` CPU fault.
- Update: do not claim exactness from the tiny test. Require coherent, repeatable
  output from the full preconverted checkpoint as the decisive correctness gate.

### E4: CUDA backend correctness

- Action: built for `sm_120` with CUDA 12.9 and supplied NixOS's live driver library
  at `/run/opengl-driver/lib`.
- Result: q8, q4, q2, and f32 CUDA correctness tests passed independently on both
  RTX 5090s.
- Update: U1 resolved. Preserve the driver-library path in the eventual service.

### E5: Colibri-shaped SSD I/O

- Action: created a 4 GiB incompressible direct-I/O fixture on `/games`, then ran
  `iobench FILE 19 256 8 1`.
- Result: 5.95 GB/s across 5.1 GB of O_DIRECT random reads, or 3.3 effective
  milliseconds per 19 MB block. This clears the 3 GB/s stop threshold and sits at
  the planned 6 GB/s green boundary.
- Update: U2 resolved for storage with the same local-ZFS/NVMe characteristics. The
  final dedicated filesystem still needs the same benchmark.

### E6: Synthetic architecture fixture

- Action: ran the 1.25 GB random architecture fixture on GPU 0 for three repetitions
  in each placement mode.
- Result: median 3.39 tok/s CPU-streamed, 3.93 tok/s with dense CUDA, 17.95 tok/s
  with CUDA-pinned experts, and 65.88 tok/s with pinned experts plus dense CUDA.
  The upstream parser needed a disposable compatibility fix for its newer profile
  output format.
- Update: the expected acceleration path works. These values are scaling checks, not
  predictions for the 753B model.

- GPU 1 repeat: its first run in each mode matched GPU 0 (3.23, 3.70, 3.45,
  17.60, and 66.64 tok/s respectively). Later repetitions slowed by roughly 2x while
  the eight-worker checkpoint transfer was active. This establishes that GPU 1 works
  and also shows that host contention must be controlled during the full benchmark.

### E7: Checkpoint staging

- Action: queried `mateogrgic/GLM-5.2-colibri-int4-with-int8-mtp` with `hf download
--dry-run`, then started a resumable download under `/tmp`.
- Result: 150 files total 383.8 GB. `/tmp` had 462 GB free; `/games` had only 349 GB
  free. The first quiet transfer exited after 696 MB, and a visible resumable retry
  was started to retain diagnostics.
- Update: download latency now overlaps the remaining probes without consuming the
  Kubernetes/OpenEBS SSD pool.

### E8: Dedicated host SSD

- Action: added a 500 GB `local-zfs` `virtio8` disk to the declarative Proxmox VM
  shape, declared `/dev/vdi` as an auto-formatted ext4 filesystem at
  `/var/lib/colibri`, and performed the documented one-time Proxmox hotplug.
- Result: atlas had about 1.55 TiB free in `local-zfs`; the blank disk appeared at
  the expected guest path, was formatted as ext4, and mounted with about 492 GB
  usable. Backup and replication are disabled because the model is reproducible.
- Update: U3 resolved without borrowing `/games` or Kubernetes/OpenEBS storage.

### E9: Final-filesystem I/O gate

- Action: repeated the 4 GiB incompressible direct-write seed and Colibri O_DIRECT
  access-pattern benchmark on `/var/lib/colibri`.
- Result: 4.89 GB/s direct write and 7.39 GB/s across 5.1 GB of 19 MB random reads,
  or 2.7 effective milliseconds per block.
- Update: the exact model filesystem is firmly in the green range. Stop the HDD
  staging transfer, copy its Hugging Face local-dir resume state to this filesystem,
  and resume the download directly on the SSD.

## 6. Current Posterior State

- A staged host experiment remains justified: CUDA and the SSD access pattern pass,
  raising the probability that it runs coherently to roughly 85%, while useful
  full-model throughput remains roughly 55% until measured.
- The dedicated 500 GB host SSD has enough capacity and exceeds the I/O gate. A
  future Kubernetes deployment can separately use the existing SSD-backed OpenEBS
  storage class instead of coupling the pod to this experiment mount.

## 7. Action Queue

1. Finish copying the partial Hugging Face local-dir state to the dedicated SSD and
   resume the checkpoint download there.
2. Run `coli plan` and `coli doctor` against the completed checkpoint.
3. Run fixed-prompt cold/warm decode
   without concurrent download or other heavy host work.
4. If coherent warm decode is at least 0.5 tok/s, test `coli serve` through LiteLLM.

## 8. Decision Tree

```text
fixture fails
└─ stop and diagnose upstream/runtime compatibility
fixture passes
├─ SSD <3 GB/s Colibri-shaped reads -> stop or attach faster dedicated storage
└─ SSD >=3 GB/s
   ├─ no safe ~380 GB allocation -> add dedicated SSD allocation first
   └─ capacity available -> convert model, run 32-token cold/warm benchmark
      ├─ warm <0.5 tok/s -> archive experiment
      └─ warm >=0.5 tok/s -> expose as glm-5.2-local behind LiteLLM
```

## 9. Stopping Criteria

- Stop before full-model execution if the final SSD benchmark falls below 3 GB/s or
  no safely owned 450+ GB SSD allocation can be made. The download may remain staged
  on HDD because it is resumable and does not commit cluster storage.
- Stop after full-model testing if coherent warm decode remains below 0.5 tok/s.
- Continue toward LiteLLM if the server is stable and warm decode reaches at least
  0.5 tok/s; treat 1 tok/s or higher as a strong success.

## 10. Vibes Ledger

- U4 throughput prior is [VIBE]. De-vibe with the upstream fixture, exact I/O
  benchmark, and a fixed 32-token cold/warm full-model run.
