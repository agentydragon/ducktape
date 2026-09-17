# llama.cpp's ggml-openvino backend (docs/backend/OPENVINO.md) translates
# supported GGML ops into an OpenVINO graph and runs it on whichever device
# OpenVINO targets -- CPU, GPU, or (what we actually want) the NPU. Building it
# needs an OpenVINO install exposing find_package(OpenVINO)'s CMake config --
# openvino-npu.nix is that install, rebuilt with a working NPU compiler.
{
  lib,
  llama-cpp,
  openvino-npu,
  onetbb,
  ocl-icd,
  opencl-headers,
  opencl-clhpp,
  autoPatchelfHook,
}:
llama-cpp.overrideAttrs (old: {
  cmakeFlags = old.cmakeFlags ++ [
    (lib.cmakeBool "GGML_OPENVINO" true)
    (lib.cmakeFeature "OpenVINO_DIR" "${openvino-npu}/runtime/cmake")
  ];

  buildInputs = old.buildInputs ++ [
    openvino-npu
    ocl-icd
    opencl-headers
    opencl-clhpp
  ];

  # openvino-npu's libopenvino.so lives at the non-standard runtime/lib/intel64
  # (CMake links it there via an imported target's absolute IMPORTED_LOCATION,
  # not a -L/-l pair), so the generic nixpkgs per-`-L`-flag rpath mechanism that
  # already covers onetbb never sees it. autoPatchelfHook finds and patches it
  # in regardless of that layout.
  nativeBuildInputs = old.nativeBuildInputs ++ [ autoPatchelfHook ];

  # ggml/src/ggml-openvino/CMakeLists.txt hardcodes
  # `include("${OpenVINO_DIR}/../3rdparty/tbb/lib/cmake/TBB/TBBConfig.cmake")`,
  # assuming Intel's official toolkit archive layout, which vendors its own TBB
  # under <install-root>/3rdparty/tbb/. openvino-npu instead links nixpkgs'
  # system onetbb (openvino's own ENABLE_SYSTEM_TBB=true), so that path doesn't
  # exist -- repoint the include at onetbb's dev output, which has the same
  # lib/cmake/TBB/TBBConfig.cmake layout.
  postPatch = (old.postPatch or "") + ''
    sed -i \
      -e 's|include(".*3rdparty/tbb/lib/cmake/TBB/TBBConfig\.cmake")|include("${lib.getDev onetbb}/lib/cmake/TBB/TBBConfig.cmake")|' \
      ggml/src/ggml-openvino/CMakeLists.txt
    grep -q '${lib.getDev onetbb}/lib/cmake/TBB/TBBConfig.cmake' ggml/src/ggml-openvino/CMakeLists.txt
  '';

  # Upstream's postInstall generates bash completion by executing the just-
  # linked llama-server binary -- but that runs before fixupPhase (where
  # autoPatchelfHook patches in openvino-npu's rpath), so the binary can't find
  # libopenvino.so yet and the build fails. A systemd unit invoking llama-server
  # directly has no use for its bash completion, so drop that step rather than
  # chase a working LD_LIBRARY_PATH for a build-time self-exec; keep upstream's
  # other postInstall step (staging llama.h for downstream consumers).
  postInstall = ''
    mkdir -p $out/include
    cp $src/include/llama.h $out/include/
  '';
})
