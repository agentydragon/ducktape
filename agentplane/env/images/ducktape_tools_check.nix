# Exercise the same preinstalled tools as the image, with no Docker daemon or network.
{ pkgs, tools }:
pkgs.runCommand "environment-tools-check"
  {
    nativeBuildInputs = [
      tools
      pkgs.git
    ];
    dontAddPythonPath = true;
    # pygit2 initializes TLS on import, even for bbr --help. The build sandbox has no /etc CA file.
    # This is check-local: the deployed container must keep its injected egress CA settings.
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
  }
  ''
    export HOME="$TMPDIR/home"
    mkdir -p "$HOME" work
    cd work
    git init -q
    cp ${../../../.pre-commit-config.yaml} .pre-commit-config.yaml
    cp ${../../../ruff.toml} ruff.toml
    chmod u+w .pre-commit-config.yaml ruff.toml

    bbr --help
    pre-commit --version
    printf 'answer = 42\n' > probe.py
    printf '# Probe\n' > probe.md
    printf '{ }: { answer = 42; }\n' > probe.nix
    printf 'exports_files(["probe.py"])\n' > BUILD.bazel
    git add .

    # Normalize the fixture first; then require the actual repository hooks to pass unchanged.
    ruff format --config ruff.toml probe.py
    prettier --write probe.md
    nixfmt probe.nix
    buildifier BUILD.bazel
    git add .
    pre-commit run ruff-format --files probe.py
    pre-commit run prettier --files probe.md
    pre-commit run nixfmt --files probe.nix
    pre-commit run buildifier --files BUILD.bazel
    git diff --exit-code
    touch "$out"
  ''
