# public-coder-devbox - headless NixOS VM used by the public-coder OpenClaw
# instance for Git checkouts, direnv, Bazel, BuildBuddy, and tests.
#
# Ephemeral KubeVirt containerDisk root (flake output
# public-coder-devbox-container-disk, published by
# .github/workflows/public-coder-devbox-image.yml and kept current by Flux image
# automation) -- nothing written to "/" survives an image update or VM restart.
# Accepted: Bazel/BuildBuddy already caches remotely, and this VM's whole point is
# to always run the current devel config, not to carry local state.
#
# The VM's egress is fenced at the KubeVirt virt-launcher Pod: DNS and the
# public-coder-agent iron-proxy are the only allowed destinations. The proxy CA
# is not copied into Git. trust-manager publishes the live CA bundle as a
# ConfigMap, KubeVirt attaches that ConfigMap as a read-only guest disk, and
# the service below assembles the runtime CA bundle at boot.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  keys = import ../../../ssh-keys.nix;
  proxyHost = "public-coder-agent-proxy.public-coder-agent.svc.cluster.local";
  proxyUrl = "http://${proxyHost}:8080";
  hostexecdTokenDevice = "/dev/disk/by-id/virtio-pctoken";
  hostexecdTokenFile = "/etc/hostexecd-daemon-token.txt";
  proxyCaDevice = "/dev/disk/by-id/virtio-pcproxyca";
  proxyCaRuntimeDir = "/run/public-coder-devbox-proxy-ca";
in
{
  imports = [
    ../../modules/vm-hardware.nix
    ../../modules/bazel
    ../../modules/hostexecd.nix
  ];

  # hostexecd needs no SSH host key at all -- it never establishes an SSH session, only outbound
  # HTTPS -- and this VM's root disk is already ephemeral (containerDisk), so a *persisted* SSH
  # host key would only buy back a stable known_hosts fingerprint across restarts that now happen
  # on every image update. sshd is left to generate its own ephemeral host key each boot, same as
  # any other fresh install.
  #
  # hostexecd's own daemon token (cluster/k8s/haku/console/node-daemon-public-coder-devbox.sops.yaml)
  # needs no on-guest sops/age decryption either, unlike wyrm2/rugged/atlas: those are physical
  # machines with no Kubernetes relationship to the cluster, so decrypting that committed ciphertext
  # themselves via a persisted host-derived age identity is their only channel. This VM is a
  # KubeVirt-managed guest Kubernetes already controls, so it gets the same treatment as the proxy
  # CA below: Flux/kustomize-controller decrypts the token server-side (it already needs to, to
  # materialize haku-console's own copy of it) and KubeVirt attaches the plaintext result as a small
  # virtio disk. No local decryption identity to persist at all.
  systemd.services.public-coder-devbox-hostexecd-token = {
    description = "Install the public-coder-devbox hostexecd daemon token";
    wantedBy = [
      "hostexecd.service"
      "multi-user.target"
    ];
    before = [ "hostexecd.service" ];
    after = [ "local-fs.target" ];
    path = [
      pkgs.coreutils
      pkgs.util-linux
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -eu
      src="/run/public-coder-devbox-hostexecd-token/source"
      mkdir -p "$src"
      mounted=0
      for _ in $(seq 1 60); do
        if mountpoint -q "$src"; then
          mounted=1
          break
        fi
        if mount -o ro "${hostexecdTokenDevice}" "$src" 2>/dev/null; then
          mounted=1
          break
        fi
        sleep 1
      done
      if [ "$mounted" -ne 1 ]; then
        echo "KubeVirt hostexecd-token disk did not appear at ${hostexecdTokenDevice}" >&2
        exit 1
      fi
      install -Dm0600 "$src/token" "${hostexecdTokenFile}"
      umount "$src"
    '';
  };

  # The VM is intentionally a root-administered build box. Its egress is
  # still enforced outside the guest by the Cilium policy on virt-launcher.
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  users.users.root.openssh.authorizedKeys.keys = [ keys.publicCoderDevbox ];

  users.users.coder = {
    isNormalUser = true;
    home = "/home/coder";
    shell = pkgs.zsh;
    openssh.authorizedKeys.keys = [ keys.publicCoderDevbox ];
  };

  environment.systemPackages = with pkgs; [
    htop
    btop
    ripgrep
    fd
    fzf
    jq
    yq
    tree
    pv
    strace
    lsof
    git
    openssl
  ];

  # The ConfigMap is attached by KubeVirt as a small virtio disk with the
  # stable serial `pcproxyca`. Build a complete CA bundle from the live
  # ConfigMap contents rather than committing a generated certificate.
  systemd.services.public-coder-devbox-proxy-ca = {
    description = "Install the live public-coder-agent proxy CA bundle";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];
    before = [ "network-online.target" ];
    path = [
      pkgs.coreutils
      pkgs.util-linux
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -eu
      src="${proxyCaRuntimeDir}/source"
      mkdir -p "$src" "${proxyCaRuntimeDir}"
      mounted=0
      for _ in $(seq 1 60); do
        if mountpoint -q "$src"; then
          mounted=1
          break
        fi
        if mount -o ro "${proxyCaDevice}" "$src" 2>/dev/null; then
          mounted=1
          break
        fi
        sleep 1
      done
      if [ "$mounted" -ne 1 ]; then
        echo "KubeVirt proxy CA ConfigMap disk did not appear at ${proxyCaDevice}" >&2
        exit 1
      fi
      test -s "$src/ca-certificates.crt"
      install -Dm0644 "$src/ca-certificates.crt" "${proxyCaRuntimeDir}/proxy-ca.crt"
      cat /etc/ssl/certs/ca-bundle.crt "${proxyCaRuntimeDir}/proxy-ca.crt" \
        > "${proxyCaRuntimeDir}/ca-bundle.crt"
      umount "$src"
    '';
  };

  # Nix's HTTP proxy is an environment setting, not a nix.conf setting. The
  # interactive environment receives these through sessionVariables, while
  # nix-daemon needs them explicitly in its systemd environment because it is
  # the process that downloads substituters and flake inputs.
  nix.settings."ssl-cert-file" = "${proxyCaRuntimeDir}/ca-bundle.crt";
  systemd.services.nix-daemon = {
    requires = [ "public-coder-devbox-proxy-ca.service" ];
    after = [ "public-coder-devbox-proxy-ca.service" ];
    environment = {
      HTTP_PROXY = proxyUrl;
      HTTPS_PROXY = proxyUrl;
      http_proxy = proxyUrl;
      https_proxy = proxyUrl;
      NO_PROXY = "127.0.0.1,localhost";
      no_proxy = "127.0.0.1,localhost";
      SSL_CERT_FILE = "${proxyCaRuntimeDir}/ca-bundle.crt";
      NIX_SSL_CERT_FILE = "${proxyCaRuntimeDir}/ca-bundle.crt";
    };
  };

  # hostexecd: haku-console runs approved public-coder-agent shell calls here, auto-approved only
  # for this exact host (haku/docs/security.md invariant #9, cluster/k8s/haku/console/config.yaml's
  # `hostexec_public_coder_devbox` policy). Its outbound HTTPS is fenced through the same iron-proxy
  # as everything else on this VM, so it needs the proxy plus the proxy's own interception CA
  # (nix/nixos/modules/hostexecd.nix's extra_root_cert_file), which every other hostexec host
  # (wyrm2/rugged/atlas) leaves unset because it reaches the console directly.
  ducktape.hostexec = {
    enable = true;
    httpsProxy = proxyUrl;
    extraRootCertFile = "${proxyCaRuntimeDir}/ca-bundle.crt";
    daemonTokenFile = hostexecdTokenFile;
  };
  systemd.services.hostexecd = {
    requires = [
      "public-coder-devbox-proxy-ca.service"
      "public-coder-devbox-hostexecd-token.service"
    ];
    after = [
      "public-coder-devbox-proxy-ca.service"
      "public-coder-devbox-hostexecd-token.service"
    ];
  };

  # These are intentionally placeholders / non-secret routing settings. The
  # iron-proxy substitutes the real GitHub credential only on GitHub hosts.
  environment.sessionVariables = {
    HTTP_PROXY = proxyUrl;
    HTTPS_PROXY = proxyUrl;
    http_proxy = proxyUrl;
    https_proxy = proxyUrl;
    NO_PROXY = "127.0.0.1,localhost";
    no_proxy = "127.0.0.1,localhost";
    GH_PAT = "proxy-github-placeholder";
    SSL_CERT_FILE = "${proxyCaRuntimeDir}/ca-bundle.crt";
    NIX_SSL_CERT_FILE = "${proxyCaRuntimeDir}/ca-bundle.crt";
    CURL_CA_BUNDLE = "${proxyCaRuntimeDir}/ca-bundle.crt";
    GIT_SSL_CAINFO = "${proxyCaRuntimeDir}/ca-bundle.crt";
    NODE_EXTRA_CA_CERTS = "${proxyCaRuntimeDir}/ca-bundle.crt";
  };

  users.motd = "public-coder-devbox - NixOS development VM for public-coder-agent\n";
}
