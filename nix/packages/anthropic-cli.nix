# Anthropic CLI (`ant`): the Claude API / Managed Agents control-plane CLI.
# Not in nixpkgs (and `pkgs.ant` is Apache Ant), so we vendor the upstream
# statically-linked release binary. Used by haku/runtime/x/managed_agent/self_hosted
# (`ant beta:{environments,agents,vaults,deployments,worker} ...`).
{
  lib,
  pkgs,
}:
pkgs.stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "anthropic-cli";
  version = "2.7.1";

  src = pkgs.fetchurl {
    url = "https://github.com/anthropics/anthropic-cli/releases/download/v${finalAttrs.version}/ant_${finalAttrs.version}_linux_amd64.tar.gz";
    hash = "sha256-fEv1CBUy2ZQgG8NnhlfI8xTkh8Uh7FOTfRXArKJcK/A=";
  };

  # Tarball holds `ant` plus completions/ and man/ at the root; the binary is
  # statically linked, so no autoPatchelf is needed.
  dontUnpack = true;
  nativeBuildInputs = [ pkgs.installShellFiles ];

  installPhase = ''
    runHook preInstall
    tar xzf "$src"
    install -Dm755 ant "$out/bin/ant"
    installShellCompletion --cmd ant \
      --bash completions/ant.bash \
      --fish completions/ant.fish \
      --zsh completions/ant.zsh
    install -Dm644 man/man1/ant.1.gz "$out/share/man/man1/ant.1.gz"
    runHook postInstall
  '';

  meta = {
    description = "Anthropic CLI (ant) — Claude API and Managed Agents control plane";
    homepage = "https://github.com/anthropics/anthropic-cli";
    license = lib.licenses.unfree;
    mainProgram = "ant";
    platforms = [ "x86_64-linux" ];
  };
})
