{
  pkgs,
  pkgsUnstable,
  ducktapePkgs,
  ruffLatest,
  rustToolchain,
}:

let
  llvmAddr2line = pkgs.llvmPackages.bintools-unwrapped;
  leanFmt = import ../packages/lean-fmt.nix { inherit pkgsUnstable; };
  # The BuildBuddy runner VM drives Bazel CI; Bazel actions run in the separate
  # RBE container image. These are the only Nix tools its scripts need.
  buildBuddyRunnerPackages = {
    inherit (ducktapePkgs) bb;
    inherit (pkgs)
      bazelisk
      time
      valgrind
      ;
    bazelDiff = ducktapePkgs.bazel-diff;
    # Optional Debundle profiling runs on the outer BuildBuddy VM, never in
    # the RBE action container. perf permissions are checked by the workflow.
    inherit (pkgs.linuxPackages) perf;
    inherit llvmAddr2line;
  };
  buildBuddyRunnerTools = builtins.attrValues buildBuddyRunnerPackages;

  # All hooks in .pre-commit-config.yaml use this shared Nix tool environment.
  preCommitPackages = [
    buildBuddyRunnerPackages.bb
    ducktapePkgs.bbr
    ducktapePkgs.ducktape-git-hooks
    ducktapePkgs.gazelle
    pkgs.pre-commit
    pkgs.python3Packages.pre-commit-hooks
    leanFmt
    buildBuddyRunnerPackages.bazelisk
    pkgs.nixfmt
    pkgs.statix
    ruffLatest
    pkgs.shfmt
    pkgs.buildifier
    pkgs.keep-sorted
    pkgs.gofumpt
    pkgs.markdownlint-cli2
    ducktapePkgs.prettier
    pkgs.kubeconform
    pkgs.tflint
    pkgs.checkov
    rustToolchain.rustfmt
    pkgs.ansible
  ];

  # Shared developer tool core used by `.#devtools` and Claude Code web's
  # Home Manager profile. The devShell and `.#devtools` also include
  # `localOnlyPackages`.
  # The Nix Attic workflow builds this so web installs can use the cache.
  # TODO: disable NLS on pre-commit's gitMinimal to drop ~31 MiB of
  # gettext + locale data. Blocked on slow rebuild (gitMinimal override
  # isn't in the binary cache, triggers 600+ derivation bootstrap chain).
  # See devinfra/claude/docs/devtools-closure-size.md for details.
  # Packages only needed by local development or cluster operations.
  localOnlyPackages = [
    # cargo-fmt is included with rustfmt but requires cargo on PATH.
    rustToolchain.cargo
    # Anthropic CLI (`ant`): Claude API / Managed Agents control plane, for
    # running `ant beta:*` (haku/runtime/x/managed_agent/self_hosted). Not included in the BuildBuddy Remote Runner toolset.
    ducktapePkgs.anthropic-cli
  ]
  ++ [
    # Keep the symbolizer available in local dev shells as well as the runner.
    llvmAddr2line
    # Cluster/infra CLIs. Used for Talos, Route 53, Nebula PKI, policy
    # validation, and bare-metal provisioning.
    pkgsUnstable.talosctl
    pkgs.awscli2 # AWS CLI for Route 53 management
    pkgs.hcloud # Price-comparison helper only; cluster bootstrap does not consume HCloud creds
    pkgs.kyverno # Policy engine CLI (validate manifests, test policies)
    pkgs.kubevirt # virtctl for KubeVirt VM consoles and lifecycle operations
    pkgs.nebula # Nebula mesh overlay (nebula-cert for PKI management)
    pkgs.ovhcloud-cli # OVH API CLI (Kimsufi server inventory, boot, IPMI)
    # Give the bootstrap provisioner a Python interpreter with the OVH client,
    # independent of which other Python interpreter appears first on PATH.
    (pkgs.writeShellScriptBin "ovh-python" ''
      exec ${pkgs.python314.withPackages (ps: [ ps.ovh ])}/bin/python3 "$@"
    '')
  ];
  # System libraries matching the RBE container image (devinfra/buildbuddy/images/rbe/Dockerfile).
  systemLibs = import ../packages/system-libs.nix { inherit pkgs; };
  # Non-hook developer tools shared by the devShell and Claude profiles.
  # Pre-commit's packages are composed once below; the BuildBuddy runner has its
  # own smaller package set above.
  devToolsCommon = [
    ducktapePkgs.bbapi
    ducktapePkgs.skills
    pkgs.openssl
    # Codex setup materializes kubeconfig via devinfra/k8s/kubeconfig.py;
    # include a guaranteed Python runtime with pyyaml for that path.
    (pkgs.python314.withPackages (ps: [ ps.pyyaml ]))
    # Infrastructure tools
    pkgs.gh
    pkgs.kubectl
    pkgsUnstable.kubectl-cnpg
    pkgs.fluxcd
    pkgs.kustomize
    pkgs.kubernetes-helm
    pkgs.opentofu
    pkgs.sops
    pkgs.ssh-to-age
  ];
  # Rust claude-hook is the active hook/shim implementation. The statusline
  # remains Python and is exposed separately as `claude-statusline`.
  devToolPackages =
    preCommitPackages
    ++ devToolsCommon
    ++ [
      ducktapePkgs.claude-hook
      ducktapePkgs.claude-statusline
    ];
in
{
  inherit
    buildBuddyRunnerTools
    localOnlyPackages
    preCommitPackages
    systemLibs
    devToolPackages
    ;
}
