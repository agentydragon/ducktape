{
  lib,
  fetchFromGitHub,
  stdenv,
  makeWrapper,
  python3,
  shaderc,
  vulkan-headers,
  vulkan-loader,
}:

stdenv.mkDerivation {
  pname = "colibri";
  version = "2.0.0";

  src = fetchFromGitHub {
    owner = "JustVugg";
    repo = "colibri";
    rev = "bf2442915d6e3dd4cdfd2eb9c2a3d2aa44a25850";
    hash = "sha256-VCrRKi0Dn7S+y/48ZlPaHckZpRx59urYQwbeSyfAJBM=";
  };

  nativeBuildInputs = [
    makeWrapper
    shaderc
  ];

  buildInputs = [
    stdenv.cc.cc.lib
    vulkan-headers
    vulkan-loader
  ];

  buildPhase = ''
    runHook preBuild
    make -C c qwen36 ARCH=x86-64-v3 VK=1 GLSLC=${lib.getBin shaderc}/bin/glslc
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -d "$out/bin" "$out/libexec/colibri/shaders" "$out/libexec/colibri/tools"
    install -m 755 c/coli "$out/libexec/colibri/coli"
    install -m 755 c/qwen36 "$out/libexec/colibri/qwen36"
    install -m 644 c/*.py "$out/libexec/colibri/"
    install -m 644 c/shaders/*.spv "$out/libexec/colibri/shaders/"
    cp -R c/tools/. "$out/libexec/colibri/tools/"

    makeWrapper ${python3}/bin/python "$out/bin/coli" \
      --add-flags "$out/libexec/colibri/coli" \
      --prefix LD_LIBRARY_PATH : "${lib.makeLibraryPath [ vulkan-loader ]}" \
      --set PYTHONPATH "$out/libexec/colibri"
    runHook postInstall
  '';

  # Vulkan is loaded with dlopen, so makeWrapper adds the loader while
  # preserving any host-provided Mesa ICD paths. The model engine stays CPU-only
  # unless callers explicitly set COLI_VULKAN=1.
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    export PYTHONDONTWRITEBYTECODE=1
    test -x "$out/libexec/colibri/qwen36"
    test -s "$out/libexec/colibri/shaders/qmatmul.spv"
    "$out/bin/coli" --version
    PYTHONPATH="$out/libexec/colibri" ${python3}/bin/python -c 'import openai_server'
    runHook postInstallCheck
  '';

  meta = {
    description = "Colibri's Qwen36 MoE engine with optional Vulkan acceleration";
    homepage = "https://github.com/JustVugg/colibri";
    license = lib.licenses.asl20;
    platforms = [ "x86_64-linux" ];
    mainProgram = "coli";
  };
}
