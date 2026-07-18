# NixOS-closure container image for the Haku Managed Agents self-hosted worker
# (Runtime B, haku/runtime/managed_agent). We build a full-NixOS rootfs (so the
# whole tool closure — bash/git/kubectl/psql/… — is consistent with the rest
# of the fleet) but we do NOT boot it: the pod runs the worker process directly.
#
# The poll loop is `worker.py` on the official anthropic Python SDK, NOT `ant`
# (the Go CLI): the Go SDK deadlocks a session on empty tool output
# (anthropic-sdk-go#377); the Python session runner guards that case. The SDK
# can't ride the Bazel lockfile (agent-framework-anthropic hard-pins anthropic
# to 0.80.0, while the worker lib needs ≥0.111), so it's pinned independently in
# the override below — see <debug/self_hosted_worker_bringup.md>.
#
# Runs UNPRIVILEGED and as NON-ROOT: k8s execs `/sw/bin/haku-managed-agent-run`
# (the wrapper below, on the stable system-path) as uid 1000 (`haku`) with all
# caps dropped. There is no systemd, no `/init`, no NixOS activation — booting
# systemd PID 1 in an unprivileged container fails to mount the API filesystems
# (/proc, /dev, /run …), and we don't need it: the closure runs fine on its own,
# and k8s already supplies supervision (restartPolicy) and log capture. The real
# fence is the haku-sandbox perimeter (haku SA + RBAC, mitmproxy egress).
#
# Build: nix build .#haku-managed-agent-image   (flake emits an uncompressed rootfs tar)
# Load:  docker import result/tarball/*.tar haku-managed-agent
# Run:   docker run --rm --user 1000 haku-managed-agent /sw/bin/haku-managed-agent-run
{
  modulesPath,
  pkgs,
  fastmcp,
  ...
}:
let
  # Haku's runtime tool surface: what reading its sources may assume on PATH. Lean
  # by design (no dev/infra toolchain — no bazelisk/tofu/helm); the worker reads
  # sources and synthesizes, it is not a dev box. Add here when a source/technique
  # needs a new tool, so the runtimes stay in deliberate sync.
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
    tea
    kubectl
    postgresql # psql (Plaid, reached cluster-internally)
    fastmcp # in-cluster MCP facades (tana-mcp-ro, …)
    # sops + age (age provides `age` and `age-keygen`) for decrypting sops files.
    # Tooling only — the age key is NOT wired into the pod yet, so decryption
    # needs SOPS_AGE_KEY (or a key file) supplied separately before it works.
    sops
    age
  ];

  # anthropic Python SDK ≥0.111 (the Managed Agents worker lib). nixpkgs ships
  # 0.75 and the repo's Bazel lockfile is held at 0.80 by agent-framework, so
  # pin it here for the worker closure only: bump the version + sdist, and add
  # docstring-parser (a runtime dep new since the nixpkgs derivation's 0.75).
  pythonWithAnthropic =
    let
      python = pkgs.python3.override {
        self = python;
        packageOverrides = _pyfinal: pyprev: {
          anthropic = pyprev.anthropic.overridePythonAttrs (old: rec {
            version = "0.111.0";
            src = pyprev.fetchPypi {
              pname = "anthropic";
              inherit version;
              hash = "sha256-OcvaCsF6bUI+W/YJgRvWmybt32KZ16RoEm4FvHEc6CY=";
            };
            dependencies = (old.dependencies or [ ]) ++ [ pyprev.docstring-parser ];
            doCheck = false;
          });
        };
      };
    in
    python.withPackages (ps: [ ps.anthropic ]);

  # worker.py is the single source of truth (self-contained + runnable on the
  # SDK). The `haku-managed-agent` command runs it under the pinned interpreter;
  # the entrypoint execs it like it used to exec `ant beta:worker poll`.
  haku-managed-agent = pkgs.writeShellApplication {
    name = "haku-managed-agent";
    runtimeInputs = [ pythonWithAnthropic ];
    text = ''exec python ${./worker.py} "$@"'';
  };

  # entrypoint.sh is the single source of truth (self-contained + runnable);
  # bake it in with its shebang patched to the store bash.
  entrypoint = pkgs.runCommand "haku-entrypoint" { } ''
    install -Dm755 ${./entrypoint.sh} $out/bin/haku-entrypoint
    patchShebangs $out/bin/haku-entrypoint
  '';

  # The pod's entry process. A thin wrapper that puts the worker tool closure
  # AND the `haku-managed-agent` poll command on PATH (so the image doesn't
  # depend on the container PATH — there's no login shell, so `/sw/bin` is NOT
  # on PATH and the entrypoint's `exec haku-managed-agent` would fail), then
  # execs the entrypoint. Added to systemPackages below, so it lands at the
  # stable path `/sw/bin/haku-managed-agent-run` — what the Deployment's
  # `command` invokes.
  haku-managed-agent-run = pkgs.writeShellApplication {
    name = "haku-managed-agent-run";
    runtimeInputs = workerTools ++ [ haku-managed-agent ];
    text = ''exec ${entrypoint}/bin/haku-entrypoint "$@"'';
  };
in
{
  imports = [ (modulesPath + "/virtualisation/docker-image.nix") ];

  networking.hostName = "haku-managed-agent";
  networking.nftables.enable = false;

  # Non-root agent user (uid 1000). The pod runs the worker as this uid via
  # securityContext.runAsUser. createHome gives a writable /home/haku in the
  # image for the entrypoint's ~/.netrc and git config. fsGroup on the pod (gid
  # 100, haku's primary `users` group) keeps the /workspace emptyDir writable.
  #
  # No systemd unit: the worker's env (ANTHROPIC_*/HAKU_* from the Deployment,
  # plus the HTTP(S)_PROXY/SSL_CERT_FILE that the haku-sandbox Kyverno policy
  # injects for mitmproxy egress) lands directly in the entry process's
  # environment — no ImportEnvironment indirection needed. k8s gives us restart
  # and log capture.
  users.users.haku = {
    isNormalUser = true;
    uid = 1000;
    home = "/home/haku";
    createHome = true;
  };

  security.pki.certificateFiles = [ "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt" ];
  environment.systemPackages = workerTools ++ [
    haku-managed-agent-run
    haku-managed-agent
  ];

  system.stateVersion = "25.11";
}
