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
  bb,
  bbr,
  ...
}:
let
  keys = import ../../../ssh-keys.nix;
  proxyHost = "public-coder-agent-proxy.public-coder-agent.svc.cluster.local";
  proxyUrl = "http://${proxyHost}:8080";
  buildbuddyKeyDevice = "/dev/disk/by-id/virtio-pcbuildbuddy";
  bazelCacheDevice = "/dev/disk/by-id/virtio-pcbazelcache";
  bazelCacheMount = "/var/cache/bazel";
  bazelOutputUserRoot = "${bazelCacheMount}/output-user-root";
  bazelRepositoryCache = "${bazelCacheMount}/repository-cache";
  bazelDiskCache = "${bazelCacheMount}/disk-cache";
  buildbuddyRuntimeDir = "/run/public-coder-devbox-buildbuddy";
  buildbuddyEnvironmentFile = "${buildbuddyRuntimeDir}/environment";
  buildbuddyBashEnvFile = "/etc/public-coder-devbox/bash-env";
  buildbuddyShellInit = ''
    if [ -r "${buildbuddyEnvironmentFile}" ]; then
      . "${buildbuddyEnvironmentFile}"
    fi
  '';
  proxyCaDevice = "/dev/disk/by-id/virtio-pcproxyca";
  proxyCaRuntimeDir = "/run/public-coder-devbox-proxy-ca";
  proxyCaBundle = "${proxyCaRuntimeDir}/ca-bundle.crt";
  # Match the complete proxy/trust client set used by sandbox setup and the
  # Console runtime. Keeping it as data avoids tool-specific variants drifting.
  proxyNetworkEnvironment = {
    HTTP_PROXY = proxyUrl;
    HTTPS_PROXY = proxyUrl;
    http_proxy = proxyUrl;
    https_proxy = proxyUrl;
    NO_PROXY = "127.0.0.1,localhost";
    no_proxy = "127.0.0.1,localhost";
  };
  proxyCaClientEnvironment = {
    SSL_CERT_FILE = proxyCaBundle;
    NIX_SSL_CERT_FILE = proxyCaBundle;
    CURL_CA_BUNDLE = proxyCaBundle;
    GIT_SSL_CAINFO = proxyCaBundle;
    # Python HTTP clients and pip may use certifi rather than SSL_CERT_FILE.
    REQUESTS_CA_BUNDLE = proxyCaBundle;
    PIP_CERT = proxyCaBundle;
    NODE_EXTRA_CA_CERTS = proxyCaBundle;
  };
  # This is an inert Haku placeholder, not a Kubernetes credential. The devbox
  # reaches haku-kubeapi through the agent iron-proxy, which substitutes the
  # caller's Agent bearer only for that exact host.
  publicCoderKubeconfig = ''
    apiVersion: v1
    kind: Config
    clusters:
      - name: haku-kubeapi
        cluster:
          server: https://haku-kubeapi.allegedly.works
          proxy-url: ${proxyUrl}
          certificate-authority: ${proxyCaBundle}
    contexts:
      - name: public-coder
        context:
          cluster: haku-kubeapi
          namespace: public-coder-agent
          user: public-coder
    current-context: public-coder
    users:
      - name: public-coder
        user:
          token: proxy-haku-console-placeholder
  '';
  sshHostKeyDevice = "/dev/disk/by-id/virtio-pchostkey";
  sshHostKeyFile = "/etc/ssh/ssh_host_ed25519_key";
in
{
  imports = [
    ../../modules/vm-hardware.nix
    ../../modules/bazel
  ];

  # NixOS's disk-image builder uses LKL's cptofs to populate the ext4 image.
  # Upstream cptofs hardcodes a 100 MiB guest and OOMs while copying this 50 GiB
  # root filesystem; raise that build-only guest limit without changing VM RAM.
  nixpkgs.overlays = [
    (final: prev: {
      lkl = prev.lkl.overrideAttrs (old: {
        postPatch = (old.postPatch or "") + ''
          substituteInPlace tools/lkl/cptofs.c \
            --replace-fail 'mem=100M' 'mem=512M'
        '';
      });
    })
  ];

  # The containerDisk is ephemeral, but it must still accommodate one Ducktape
  # checkout plus the Nix inputs/tooling needed to start a remote BuildBuddy job.
  # Keep the qcow2 sparse; KubeVirt allocates blocks only as the guest writes them.
  virtualisation.diskSize = 50 * 1024;

  # The host key is persisted rather than regenerated per boot because sshpiper
  # (cluster/k8s/agents/public-coder-agent/sshpiper) is the party that verifies this upstream, and
  # its Pipe pins the key in `known_hosts_data`. A key that changes on every image update would
  # leave that pin permanently stale, and sshpiper's only alternative -- an empty known_hosts_data
  # -- means no upstream verification at all. Delivered as a guest disk, same as the two secrets
  # below.
  #
  # Exactly one host key type, deliberately: sshpiper picks one of the types the upstream offers
  # and fails with a bare `Permission denied (publickey)` if that type is missing from
  # known_hosts_data (tg123/sshpiper#554). One type means one thing to pin and no ambiguity about
  # which key was checked.
  services.openssh.hostKeys = lib.mkForce [
    {
      path = sshHostKeyFile;
      type = "ed25519";
    }
  ];

  systemd.services.public-coder-devbox-ssh-host-key = {
    description = "Install the public-coder-devbox sshd host key";
    wantedBy = [ "multi-user.target" ];
    # sshd-keygen generates any host key that is still missing; landing the real key first is what
    # stops it from minting a throwaway one. Ordering against a unit that does not exist on this
    # nixpkgs is a no-op, so naming both generations of the NixOS unit costs nothing.
    before = [
      "sshd.service"
      "sshd-keygen.service"
    ];
    after = [ "local-fs.target" ];
    path = [
      pkgs.coreutils
      pkgs.openssh
      pkgs.util-linux
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -eu
      src="/run/public-coder-devbox-ssh-host-key/source"
      mkdir -p "$src"
      mounted=0
      for _ in $(seq 1 60); do
        if mountpoint -q "$src"; then
          mounted=1
          break
        fi
        if mount -o ro "${sshHostKeyDevice}" "$src" 2>/dev/null; then
          mounted=1
          break
        fi
        sleep 1
      done
      if [ "$mounted" -ne 1 ]; then
        echo "KubeVirt ssh-host-key disk did not appear at ${sshHostKeyDevice}" >&2
        exit 1
      fi
      install -Dm0600 "$src/ssh_host_ed25519_key" "${sshHostKeyFile}"
      ssh-keygen -y -f "${sshHostKeyFile}" > "${sshHostKeyFile}.pub"
      chmod 0644 "${sshHostKeyFile}.pub"
      umount "$src"
    '';
  };

  # Root SSH login stays available for the human Operator (key-only). Its egress is enforced
  # outside the guest by the Cilium policy on virt-launcher.
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  users.users.root.openssh.authorizedKeys.keys = [
    keys.publicCoderDevbox
    # ssh-mcp's dedicated key for this (host, user) target (cluster/k8s/ssh-mcp), distinct from
    # the operator's own key above so revoking one never revokes the other.
    keys.publicCoderDevboxMcpRoot
  ];

  # `coder` is the unprivileged account used for public-coder-agent's SSH build sessions.
  users.users.coder = {
    isNormalUser = true;
    home = "/home/coder";
    shell = pkgs.bash;
    openssh.authorizedKeys.keys = [
      keys.publicCoderDevbox
      # sshpiper's mapping key (cluster/k8s/agents/public-coder-agent/sshpiper). Authorized here
      # and not for root: the piper re-originates the Agent's session as this account. Nothing
      # about the Pipe's own configuration is load-bearing for that.
      keys.publicCoderAgentSshpiper
      # ssh-mcp's dedicated key for this (host, user) target, same reasoning as root's above.
      keys.publicCoderDevboxMcpCoder
    ];
  };

  # Give every Git invocation in the ephemeral devbox a stable, non-interactive
  # author identity. Individual repositories can still override it locally when needed.
  programs.git.enable = true;
  programs.git.config.user = {
    name = "agentydragon-agent";
    email = "agentydragon-agent@users.noreply.github.com";
  };

  # SSH commands use non-interactive Bash, so propagate the non-secret loader path
  # there. The loader reads the credential only at runtime after the key service runs.
  services.openssh.extraConfig = ''
    Match User coder
      SetEnv BASH_ENV=${buildbuddyBashEnvFile}
      SetEnv GH_TOKEN=proxy-github-placeholder
      SetEnv KUBECONFIG=/home/coder/.kube/config
  '';

  environment.etc."public-coder-devbox/bash-env".text = buildbuddyShellInit;
  environment.loginShellInit = buildbuddyShellInit;
  programs.bash.interactiveShellInit = buildbuddyShellInit;

  # Mount the separately deletable raw cache PVC declaratively. `autoFormat` is
  # safe for this dedicated blank block PVC and makes its first attachment usable.
  fileSystems."${bazelCacheMount}" = {
    device = bazelCacheDevice;
    fsType = "ext4";
    options = [ "noatime" ];
    autoFormat = true;
    autoResize = true;
  };

  # The cache PVC is mounted during local-fs.target, before tmpfiles runs. Keep
  # its top-level mount root owned by root, but create exactly the three Bazel
  # working directories for coder; this avoids a world-writable cache volume.
  systemd.tmpfiles.rules = [
    "d ${bazelOutputUserRoot} 0700 coder users -"
    "d ${bazelRepositoryCache} 0700 coder users -"
    "d ${bazelDiskCache} 0700 coder users -"
  ];

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
    gh
    openssl
    kubectl
    # `bbr` remains the ordinary remote-BuildBuddy path and delegates to `bb`.
    # Bazelisk is available by its own name for intentional local,
    # repository-versioned Bazel execution in this isolated VM.
    bb
    bbr
    bazelisk
  ];

  # The ConfigMap is attached by KubeVirt as a small virtio disk with the
  # stable serial `pcproxyca`. Build a complete CA bundle from the live
  # ConfigMap contents rather than committing a generated certificate.
  ducktape.bazel.extraSystemBazelrc = ''
    # Share output bases, downloaded repositories, and local action results across
    # all devbox worktrees; all three live on the separately deletable cache PVC.
    startup --output_user_root=${bazelOutputUserRoot}
    common --repository_cache=${bazelRepositoryCache}
    build --disk_cache=${bazelDiskCache}
    # The embedded Bazel JVM reads this generated JKS, and the system rc is
    # reliable for non-login build invocations where ~/.bazelrc is not loaded.
    startup --host_jvm_args=-Djavax.net.ssl.trustStore=${proxyCaRuntimeDir}/bazel-cacerts
    startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=changeit
    try-import /home/coder/.config/bazel/buildbuddy.bazelrc
  '';

  systemd.services.public-coder-devbox-proxy-ca = {
    description = "Install the live public-coder-agent proxy CA bundle";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];
    before = [ "network-online.target" ];
    path = [
      pkgs.coreutils
      pkgs.gawk
      pkgs.jdk
      pkgs.util-linux
    ];
    environment.BAZEL_PROXY_TRUSTSTORE = "${proxyCaRuntimeDir}/bazel-cacerts";
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
      # Bazel's embedded JVM ignores the PEM bundle. Build an isolated JKS
      # from every live proxy-bundle certificate: it carries both the public
      # roots and the interception root, avoiding duplicate-import failures
      # against the Nix JDK's pre-populated cacerts store.
      rm -f "$BAZEL_PROXY_TRUSTSTORE"
      cert_dir="${proxyCaRuntimeDir}/java-certs"
      mkdir -p "$cert_dir"
      awk -v out="$cert_dir" '
        /BEGIN CERTIFICATE/ { n++; file = sprintf("%s/cert-%03d.pem", out, n) }
        file != "" { print > file }
        /END CERTIFICATE/ { close(file); file = "" }
      ' "${proxyCaRuntimeDir}/proxy-ca.crt"
      for cert in "$cert_dir"/*.pem; do
        keytool -importcert -noprompt -storepass changeit \
          -alias "public-coder-proxy-ca-$(basename "$cert" .pem)" \
          -keystore "$BAZEL_PROXY_TRUSTSTORE" \
          -file "$cert"
      done
      rm -rf "$cert_dir"
      umount "$src"
    '';
  };

  # The live Agentplane acceptance suite is manual/no-remote-exec and must run
  # on this controlled host. Install its non-secret kubeconfig at boot rather
  # than copying a credential from the agent pod; its placeholder is mediated
  # by the existing iron-proxy route above.
  systemd.services.public-coder-devbox-kubeconfig = {
    description = "Install the public-coder devbox Kubernetes proxy config";
    wantedBy = [ "multi-user.target" ];
    requires = [ "public-coder-devbox-proxy-ca.service" ];
    after = [
      "local-fs.target"
      "public-coder-devbox-proxy-ca.service"
    ];
    path = [ pkgs.coreutils ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -eu
      install -d -m0700 -o coder -g users /home/coder/.kube
      printf '%s\n' '${publicCoderKubeconfig}' > /home/coder/.kube/config
      chown coder:users /home/coder/.kube/config
      chmod 0600 /home/coder/.kube/config
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
    environment =
      proxyNetworkEnvironment
      // proxyCaClientEnvironment
      // {
        # nixpkgs' own nix-daemon module already defines this one, to nss-cacert's bundle. Two
        # definitions of a single environment key conflict rather than merge, so overriding the
        # shared set's value here is what lets both modules coexist.
        CURL_CA_BUNDLE = lib.mkForce proxyCaBundle;
      };
  };

  # Materialize the reflected BuildBuddy Secret only at runtime. bbr needs the
  # environment key; Bazel reads the matching credential rc imported by ~/.bazelrc.
  systemd.services.public-coder-devbox-buildbuddy = {
    description = "Install the public-coder-devbox BuildBuddy credential";
    wantedBy = [ "multi-user.target" ];
    requires = [ "public-coder-devbox-proxy-ca.service" ];
    after = [
      "local-fs.target"
      "public-coder-devbox-proxy-ca.service"
    ];
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
      src="${buildbuddyRuntimeDir}/source"
      mkdir -p "$src" "${buildbuddyRuntimeDir}"
      mounted=0
      for _ in $(seq 1 60); do
        if mountpoint -q "$src" || mount -o ro "${buildbuddyKeyDevice}" "$src" 2>/dev/null; then mounted=1; break; fi
        sleep 1
      done
      [ "$mounted" -eq 1 ] || { echo "BuildBuddy key disk missing" >&2; exit 1; }
      test -s "$src/api-key"
      umask 077
      { printf 'export BUILDBUDDY_API_KEY='; cat "$src/api-key"; printf '\n'; } > "${buildbuddyEnvironmentFile}"
      chown coder:users "${buildbuddyEnvironmentFile}"
      chmod 0600 "${buildbuddyEnvironmentFile}"
      install -d -m0700 -o coder -g users /home/coder/.config/bazel
      { printf 'common:rbe --remote_header=x-buildbuddy-api-key='; cat "$src/api-key"; printf '\n'; } > /home/coder/.config/bazel/buildbuddy.bazelrc
      chown coder:users /home/coder/.config/bazel/buildbuddy.bazelrc
      chmod 0600 /home/coder/.config/bazel/buildbuddy.bazelrc
      umount "$src"
    '';
  };

  # These are intentionally placeholders / non-secret routing settings. The
  # iron-proxy substitutes the real GitHub credential only on GitHub hosts.
  environment.sessionVariables =
    proxyNetworkEnvironment
    // proxyCaClientEnvironment
    // {
      # The GitHub CLI reads this inert proxy value; iron-proxy replaces it
      # only in Authorization headers sent to GitHub.
      GH_TOKEN = "proxy-github-placeholder";
    };

  users.motd = "public-coder-devbox - NixOS development VM for public-coder-agent\n";
}
