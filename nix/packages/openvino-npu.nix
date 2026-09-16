# nixpkgs' `openvino` builds with ENABLE_INTEL_NPU=true (the NPU plugin/dispatcher)
# but never sets ENABLE_INTEL_NPU_COMPILER, so the plugin builds without a working
# compiler backend: src/plugins/intel_npu/CMakeLists.txt gates the actual compiler
# behind that separate flag (defaults to ${BUILD_SHARED_LIBS}, which nixpkgs never
# sets explicitly either way). Force it on, and point NPU_PLUGIN_COMPILER_ROOT --
# the override src/plugins/intel_npu/cmake/download_compiler_libs.cmake's
# resolve_archive_dependency() checks before attempting any network fetch -- at the
# hermetically-fetched compiler libs from ./npu-compiler-libs.nix, so the build
# never tries its own mid-build download (which Nix's sandbox would block anyway).
{
  lib,
  openvino,
  npu-compiler-libs,
}:
openvino.overrideAttrs (old: {
  cmakeFlags = old.cmakeFlags ++ [
    (lib.cmakeBool "ENABLE_INTEL_NPU_COMPILER" true)
  ];
  env = (old.env or { }) // {
    NPU_PLUGIN_COMPILER_ROOT = "${npu-compiler-libs}";
  };
})
