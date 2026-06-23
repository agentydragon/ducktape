# Full-NixOS container image for the Haku Managed Agents self-hosted worker
# (Runtime B, haku/runtime/managed_agent). systemd PID 1, declaratively
# consistent with the rest of the fleet (wyrm2/rugged/nix-rbe-worker).
#
# Runs UNPRIVILEGED: on the cluster's cgroup-v2 nodes (Talos) systemd boots as
# PID 1 in an ordinary non-root container — no --privileged, no extra caps. k8s
# supervises the pod; systemd here just runs the one worker unit and gives it
# journald + Restart. The real fence is the haku-sandbox perimeter (haku SA +
# RBAC, mitmproxy egress), not anything inside the container.
#
# Build: nix build .#haku-worker-image
# Load:  docker import result/tarball/*.tar.xz haku-worker
# Run:   docker run --rm haku-worker /init      (k8s: command: ["/init"])
{
  modulesPath,
  pkgs,
  anthropic-cli,
  fastmcp,
  ...
}:
let
  # Haku's runtime tool surface: what playbooks may assume on PATH. Lean by
  # design (no dev/infra toolchain — no bazelisk/tofu/helm); the worker runs
  # playbooks, it is not a dev box. Add here when a playbook needs a new tool,
  # so the runtimes stay in deliberate sync.
  workerTools = with pkgs; [
    bashInteractive
    coreutils
    gnugrep
    gnused
    gawk
    findutils
    gnutar
    gzip
    which
    less
    jq
    curl
    openssl
    cacert
    git
    kubectl
    postgresql # psql (Plaid, reached cluster-internally)
    fastmcp # in-cluster MCP facades (tana-mcp-ro, …)
    anthropic-cli # `ant` — the worker poll loop
  ];

  # entrypoint.sh is the single source of truth (self-contained + runnable);
  # bake it in with its shebang patched to the store bash.
  entrypoint = pkgs.runCommand "haku-entrypoint" { } ''
    install -Dm755 ${./entrypoint.sh} $out/bin/haku-entrypoint
    patchShebangs $out/bin/haku-entrypoint
  '';
in
{
  imports = [ (modulesPath + "/virtualisation/docker-image.nix") ];

  networking.hostName = "haku-worker";
  networking.nftables.enable = false;
  nixpkgs.config.allowUnfree = true; # anthropic-cli is unfree

  # Non-root agent user (uid 1000). Set the pod's fsGroup to its primary group
  # so a /workspace emptyDir mount stays writable.
  users.users.haku = {
    isNormalUser = true;
    uid = 1000;
    home = "/home/haku";
    createHome = true;
  };

  # The worker unit: clone ducktape + haku-state, then long-poll Anthropic's
  # self-hosted work queue (ant beta:worker poll). entrypoint.sh holds the body.
  systemd.services.haku-worker = {
    description = "Haku Managed Agents self-hosted worker";
    wantedBy = [ "multi-user.target" ];
    path = workerTools;
    serviceConfig = {
      Type = "exec";
      User = "haku";
      WorkingDirectory = "/workspace";
      ExecStart = "${entrypoint}/bin/haku-entrypoint";
      Restart = "always";
      RestartSec = 5;
      # Base CA bundle for git/curl/ant. In-cluster these traverse
      # haku-mitmproxy; its CA is layered into the pod's trust by the
      # inject-haku-mitmproxy Kyverno policy (finalized with the k8s manifests).
      Environment = "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt";
      # k8s sets the worker env on the container (envFrom secret/configMap); it
      # lands in PID 1's (systemd) environment, and ImportEnvironment lifts the
      # named vars into this unit (systemd >= 254; NixOS 25.11 ships newer).
      # EnvironmentFile is an optional fallback: mount a secret as an env file.
      ImportEnvironment =
        "ANTHROPIC_ENVIRONMENT_ID ANTHROPIC_ENVIRONMENT_KEY "
        + "HAKU_DUCKTAPE_REPO_URL HAKU_STATE_REPO_URL "
        + "HAKU_GIT_HOST HAKU_GIT_USERNAME HAKU_GIT_PASSWORD";
      EnvironmentFile = "-/etc/haku-worker/env";
    };
  };

  # /workspace: the git clones + agent workdir, writable by haku. In k8s this is
  # normally an emptyDir (with the pod fsGroup above the mount is group-writable).
  systemd.tmpfiles.rules = [ "d /workspace 0750 haku users -" ];

  security.pki.certificateFiles = [ "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt" ];
  environment.systemPackages = workerTools;

  system.stateVersion = "25.11";
}
