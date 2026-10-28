# The command-line tools of an agentplane box, as one list for every kind of Sandbox: the Pod images
# (<sandbox.nix>, and the runner and build images on it) and the VM guest (<../runner/vm.nix>). A tool
# added here reaches all of them. What a kind adds of its own stays with it: the Pod images their FHS
# bash, CA bundle and `agentplane` command, the guest its disk tools and harnesses.
pkgs: with pkgs; [
  coreutils
  findutils
  gnugrep
  gnused
  gawk
  diffutils
  gnupatch
  file
  less
  procps
  which
  gnutar
  gzip
  xz
  unzip

  curl
  git
  ripgrep
  jq
  openssl
  kubectl # the sandbox's own Kubernetes identity, through the egress proxy
  (import ../runner/key_file_sync.nix { inherit pkgs; })

  # FULL python3, never `python3Minimal`, whose missing `json`/`shutil` broke the Haku image's
  # scripts. nixpkgs marks it EXTERNALLY-MANAGED, so a run installs packages into a
  # `python3 -m venv`, whose ensurepip brings its own pip.
  python3
]
