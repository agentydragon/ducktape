# Nix-built OCI image for the codex pod (see cluster/k8s/agents/codex-pod/README.md).
#
# Two parts, both from Nix — no runtime bootstrap script:
#   - the tool set is a single `buildEnv` (codexEnv) on /bin;
#   - the user's static dotfiles come from a home-manager config (home.nix), whose
#     home-files are baked into /home/codex at build time.
# Secrets are delivered by k8s at runtime (BUILDBUDDY_API_KEY env, the id_ed25519
# plant, ESO-templated files) — so no sops-nix, no systemd, non-root UID 1000.
#
# Still a dockerTools archive (not a NixOS tarball), so CI pushes it with the same
# `skopeo copy docker-archive:` path. Flux image automation rolls the Deployment.
#
# Build:  nix build .#codex-pod-image
# Load:   docker load < result
{
  pkgs,
  pkgsUnstable,
  home-manager,
}:
let
  # The codex pod's tool set. Mirrors the agent-box codex user plus the shell
  # utilities a dev pod needs.
  codexEnv = pkgs.buildEnv {
    name = "codex-env";
    paths = [
      pkgsUnstable.codex # Codex CLI (unstable, as home-manager pins it for agent-box)
      pkgs.bashInteractive
      pkgs.coreutils
      pkgs.moreutils
      pkgs.findutils
      pkgs.gnugrep
      pkgs.gnused
      pkgs.gawk
      pkgs.gnutar
      pkgs.gzip
      pkgs.which
      pkgs.psmisc
      pkgs.git
      pkgs.tea
      pkgs.openssh # ssh client + sshd (inbound `ssh codex-pod` over kubectl exec)
      pkgs.socat # relay for the ssh-over-kubectl-exec ProxyCommand
      pkgs.tini # PID 1 init: reaps zombies from exec/ssh sessions, forwards signals
      pkgs.curl
      pkgs.jq
      pkgs.ripgrep
      pkgs.fd
      pkgs.nodejs_22
      pkgs.nix
      pkgs.direnv
      pkgs.nix-direnv
      pkgs.kubectl
      pkgs.cacert
    ];
    # Only /bin and /share: /etc + /home are created in fakeRootCommands.
    pathsToLink = [
      "/bin"
      "/share"
    ];
  };

  homeConfig = home-manager.lib.homeManagerConfiguration {
    inherit pkgs;
    modules = [ ./home.nix ];
  };
  # home-manager's realized dotfiles tree (~/.ssh/config, ~/.bashrc, direnv, …).
  homeFiles = homeConfig.config.home-files;
  # The generated ~/.bashrc sources ~/.nix-profile/etc/profile.d/hm-session-vars.sh,
  # which only exists after a real `home-manager switch`. This package holds that
  # file; symlinking ~/.nix-profile at it (below) makes the source succeed instead
  # of erroring on every login shell.
  homeSessionVars = homeConfig.config.home.sessionVariablesPackage;
in
pkgs.dockerTools.buildLayeredImage {
  name = "codex-pod";
  # Content-addressed tag; CI adds a sortable tag for Flux ImagePolicy selection.
  tag = null;

  contents = codexEnv;
  maxLayers = 100;

  enableFakechroot = true;
  fakeRootCommands = ''
    mkdir -p tmp workspace etc/ssl/certs
    chmod 1777 tmp

    # Bake the home-manager dotfiles into /home/codex (HOME; the /workspace PVC is
    # mounted elsewhere so it doesn't shadow these). home-manager leaves these dirs
    # 0555; make them writable so the entrypoint can plant id_ed25519 into ~/.ssh
    # and Codex can write history/sessions/auth into ~/.codex.
    mkdir -p home/codex
    cp -a ${homeFiles}/. home/codex/
    chmod 700 home/codex/.ssh home/codex/.codex

    # ~/.bashrc sources ~/.nix-profile/etc/profile.d/hm-session-vars.sh; point the
    # profile at the session-vars package so that source succeeds (no activation here).
    ln -s ${homeSessionVars} home/codex/.nix-profile

    # Replace the read-only /nix/store config.toml symlink with a writable real copy:
    # the `codex` shell wrapper (home.nix) appends per-directory trust tables to it so
    # codex never shows its directory-trust prompt.
    cp --remove-destination "$(readlink -f home/codex/.codex/config.toml)" home/codex/.codex/config.toml
    chmod 644 home/codex/.codex/config.toml

    chown -R 1000:1000 home/codex workspace

    cat > etc/passwd <<'PASSWD'
    root:x:0:0:root:/root:/bin/bash
    codex:x:1000:1000:Codex:/home/codex:/bin/bash
    nobody:x:65534:65534:Nobody:/:/noshell
    PASSWD
    cat > etc/group <<'GROUP'
    root:x:0:
    codex:x:1000:
    nogroup:x:65534:
    GROUP
    cat > etc/nsswitch.conf <<'NSS'
    passwd: files
    group: files
    hosts: files dns
    NSS
    ln -sf ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-certificates.crt
  '';

  config = {
    Env = [
      "PATH=/bin:${codexEnv}/bin"
      "HOME=/home/codex"
      "USER=codex"
      # Codex reads its baked config.toml from the default CODEX_HOME (~/.codex);
      # don't override it. Build caches persist on the /workspace PVC.
      "XDG_CACHE_HOME=/workspace/.cache"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
      "NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
      "GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt"
      "NIX_CONFIG=experimental-features = nix-command flakes\naccept-flake-config = true"
    ];
    # Source-code provenance (OCI image.source) — the image lives in our Forgejo registry.
    Labels."org.opencontainers.image.source" = "https://git.allegedly.works/agentydragon/ducktape";
    # The Deployment sets the command (plant the SSH key, then run). Default to a
    # shell for `docker run` / `kubectl exec`.
    Cmd = [ "/bin/bash" ];
  };
}
