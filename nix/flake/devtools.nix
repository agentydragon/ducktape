{
  pkgs,
  pkgsUnstable,
  ducktapePkgs,
  ruffLatest,
}:

let
  # Dev tools shared between the devShell (local `nix develop` / direnv)
  # and Claude Code web (`nix profile install .#devtools`).
  # release.yml pushes this to attic so web installs are cache hits.
  # TODO: disable NLS on pre-commit's gitMinimal to drop ~31 MiB of
  # gettext + locale data. Blocked on slow rebuild (gitMinimal override
  # isn't in the binary cache, triggers 600+ derivation bootstrap chain).
  # See devinfra/claude/docs/devtools-closure-size.md for details.
  # Packages not needed by the BuildBuddy Remote Runner toolset (large, local/infra-only).
  # Excluded from rbetools to keep the BuildBuddy Remote Runner image small.
  localOnlyPackages = [
    # Anthropic CLI (`ant`): Claude API / Managed Agents control plane, for
    # running `ant beta:*` (haku/runtime/managed_agent/self_hosted). Not included in the BuildBuddy Remote Runner toolset.
    ducktapePkgs.anthropic-cli
    pkgs.rustfmt # 1GB (pulls full rustc via RPATH)
    pkgs.ansible # 650MB
    # llvm-addr2line: drop-in for GNU addr2line used by `perf report` for
    # inline-frame symbolization. 10-50x faster on Rust DWARF and keeps a
    # persistent symbol cache across queries from the same process; the
    # debundle perf-profile wrapper (devinfra/js/debundle/pipeline.bzl)
    # prepends a shim that aliases addr2line -> llvm-addr2line when it is
    # on PATH.
    pkgs.llvmPackages.bintools-unwrapped
    # Cluster/infra CLIs (formerly cluster/shell.nix). Used for Talos,
    # Route 53, Nebula PKI, policy validation, and bare-metal provisioning.
    pkgsUnstable.talosctl
    pkgs.awscli2 # AWS CLI for Route 53 management
    pkgs.hcloud # Price-comparison helper only; cluster bootstrap does not consume HCloud creds
    pkgs.kyverno # Policy engine CLI (validate manifests, test policies)
    pkgs.nebula # Nebula mesh overlay (nebula-cert for PKI management)
    pkgs.ovhcloud-cli # OVH API CLI (Kimsufi server inventory, boot, IPMI)
    pkgs.python314Packages.ovh # OVH Python client for ad-hoc API scripts
  ];
  # System libraries matching the RBE container image (devinfra/rbe_container_image/Dockerfile).
  systemLibs = import ../packages/system-libs.nix { inherit pkgs; };
  # Common dev tools used alongside Claude hook dispatch and the Python statusline.
  devToolsCommon = [
    ducktapePkgs.bb
    ducktapePkgs.bbapi
    ducktapePkgs.bbr
    ducktapePkgs.ducktape-git-hooks
    # Repo-configured Gazelle; `gazelle` / `gazelle -mode=diff` from a
    # checkout regenerate Python BUILD files without Bazel.
    ducktapePkgs.gazelle
    ducktapePkgs.skills
    # Dev tools
    pkgs.pre-commit
    pkgs.bazelisk
    pkgs.nixfmt
    pkgs.statix
    ruffLatest
    pkgs.shfmt
    pkgs.buildifier
    pkgs.keep-sorted
    pkgs.gofumpt
    pkgs.markdownlint-cli2
    ducktapePkgs.prettier
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
    pkgs.kubeconform
    pkgs.opentofu
    pkgs.tflint
    pkgs.checkov # Terraform security scanner; backs the checkov_diff pre-commit hook
    pkgs.sops
    pkgs.ssh-to-age
    ducktapePkgs.bazel-diff
  ];
  # Rust claude-hook is the active hook/shim implementation. The statusline
  # remains Python and is exposed separately as `claude-statusline`.
  devToolPackages = devToolsCommon ++ [
    ducktapePkgs.claude-hook
    ducktapePkgs.claude-statusline
  ];
in
{
  inherit
    localOnlyPackages
    systemLibs
    devToolPackages
    ;
}
