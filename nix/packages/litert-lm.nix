{
  lib,
  stdenv,
  autoPatchelfHook,
  fetchPypi,
  vulkan-loader,
  python3Packages,
}:
let
  litert-lm-api = python3Packages.buildPythonPackage rec {
    pname = "litert-lm-api";
    version = "0.17.0";
    format = "wheel";

    src = fetchPypi {
      pname = "litert_lm_api";
      inherit version format;
      dist = "py3";
      python = "py3";
      abi = "none";
      platform = "manylinux_2_27_x86_64";
      hash = "sha256-ZU7+fdaHAzWzz6vlfevJ7xO+7OFuSYGnHJZNOiEvtyQ=";
    };

    nativeBuildInputs = [ autoPatchelfHook ];
    buildInputs = [
      stdenv.cc.cc.lib
      vulkan-loader
    ];

    pythonImportsCheck = [ "litert_lm" ];

    meta = {
      description = "Python bindings for LiteRT-LM";
      homepage = "https://github.com/google-ai-edge/LiteRT-LM";
      license = lib.licenses.asl20;
      platforms = [ "x86_64-linux" ];
    };
  };

  litert-lm-builder = python3Packages.buildPythonPackage rec {
    pname = "litert-lm-builder";
    version = "0.17.0";
    format = "wheel";

    src = fetchPypi {
      pname = "litert_lm_builder";
      inherit version format;
      dist = "py3";
      python = "py3";
      abi = "none";
      platform = "any";
      hash = "sha256-iFqAcZ1OPV6WPRZXxRDct+m6zsT9FyNlSaXuEu6ROwM=";
    };

    propagatedBuildInputs = with python3Packages; [
      absl-py
      flatbuffers
      protobuf
      tomli
    ];

    pythonImportsCheck = [ "litert_lm_builder" ];

    meta = {
      description = "Python tools for building and inspecting LiteRT-LM file formats";
      homepage = "https://github.com/google-ai-edge/LiteRT-LM";
      license = lib.licenses.asl20;
    };
  };
in
python3Packages.buildPythonApplication rec {
  pname = "litert-lm";
  version = "0.17.0";
  format = "wheel";

  src = fetchPypi {
    pname = "litert_lm";
    inherit version format;
    dist = "py3";
    python = "py3";
    abi = "none";
    platform = "any";
    hash = "sha256-AVvUzIs3SCET3cYt6nbM0SAJQtl9UlFKBwvfn9TZVNg=";
  };

  postInstall = ''
    patch -d "$out/${python3Packages.python.sitePackages}" -p1 \
      < ${./litert-lm-serve-speculative-decoding.patch}
  '';

  propagatedBuildInputs =
    (with python3Packages; [
      click
      huggingface-hub
      prompt-toolkit
      questionary
      typing-extensions
    ])
    ++ [
      litert-lm-api
      litert-lm-builder
    ];

  pythonImportsCheck = [ "litert_lm_cli.main" ];

  meta = {
    description = "Command-line tool for LiteRT-LM";
    homepage = "https://github.com/google-ai-edge/LiteRT-LM";
    license = lib.licenses.asl20;
    mainProgram = "litert-lm";
    platforms = [ "x86_64-linux" ];
  };
}
