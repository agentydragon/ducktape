# nixpkgs' `openvino` builds with ENABLE_INTEL_NPU=true (the NPU plugin/dispatcher)
# but never sets ENABLE_INTEL_NPU_COMPILER, so the plugin builds without a working
# compiler backend: src/plugins/intel_npu/CMakeLists.txt gates the actual compiler
# behind that separate flag. Force it on.
#
# With the flag on, src/plugins/intel_npu/cmake/download_compiler_libs.cmake needs
# two more things nixpkgs' sandboxed build can't give it directly:
#
# 1. It gates everything on `lsb_release -is`/`-rs` reporting literally "Ubuntu"
#    "22.04" or "24.04" before it does anything else. Nix's build sandbox isn't a
#    real Ubuntu install, so the real lsb_release call reports nothing matching
#    and the script silently skips the whole compiler-libs step ("different
#    Linux distribution ... skip downloading"). The postPatch below hardcodes
#    what lsb_release would have reported on the target platform (Ubuntu 24.04)
#    instead of touching the branch logic that follows it, so upstream's own
#    tested variable assignments (destination paths, the driver-to-plugin lib
#    rename) run unmodified -- only the two lsb_release calls are replaced.
#    sed, not substituteInPlace: Nix's indented ('' '') strings dedent the whole
#    block by its shallowest line, which would mangle a nested multi-line
#    literal that has to match the source file's own indentation exactly; a
#    whitespace-agnostic regex sidesteps that (CMake itself doesn't care about
#    indentation either way).
#
# 2. Past that gate, this file's download_and_extract() has no override hook at
#    all -- it unconditionally `file(DOWNLOAD)`s the prebuilt compiler archive
#    unless its target extraction directory already exists on disk, and Nix's
#    sandbox blocks that download outright (this isn't a fixed-output
#    derivation). So the postPatch pre-creates that exact directory --
#    temp/compiler_libs/ubuntu24.04/npu_compiler_vcl_ubuntu_24_04-<version>/,
#    where <version> is PLUGIN_COMPILER_VERSION-PLUGIN_COMPILER_COMMIT_SHA from
#    the same file -- populated from npu-compiler-libs.nix's hermetic fetch of
#    the identical archive, so the existence check short-circuits the download
#    and the rest of the script (configure_file's driver-lib rename, the
#    install() step) runs unmodified against those files. That directory name
#    must move in lockstep with whatever openvino release this overrides; see
#    npu-compiler-libs.nix's own header.
{
  lib,
  openvino,
  npu-compiler-libs,
}:
openvino.overrideAttrs (old: {
  cmakeFlags = old.cmakeFlags ++ [
    (lib.cmakeBool "ENABLE_INTEL_NPU_COMPILER" true)
  ];
  postPatch = (old.postPatch or "") + ''
    sed -i \
      -e 's/^[[:space:]]*execute_process(COMMAND lsb_release -is OUTPUT_VARIABLE OS_NAME OUTPUT_STRIP_TRAILING_WHITESPACE)$/    set(OS_NAME "Ubuntu")/' \
      -e 's/^[[:space:]]*execute_process(COMMAND lsb_release -rs OUTPUT_VARIABLE OS_VERSION OUTPUT_STRIP_TRAILING_WHITESPACE)$/    set(OS_VERSION "24.04")/' \
      src/plugins/intel_npu/cmake/download_compiler_libs.cmake
    grep -q 'set(OS_NAME "Ubuntu")' src/plugins/intel_npu/cmake/download_compiler_libs.cmake
    grep -q 'set(OS_VERSION "24.04")' src/plugins/intel_npu/cmake/download_compiler_libs.cmake

    compilerLibsDir=src/plugins/intel_npu/temp/compiler_libs/ubuntu24.04/npu_compiler_vcl_ubuntu_24_04-7_6_0-da3cc32
    mkdir -p "$(dirname "$compilerLibsDir")"
    cp -r ${npu-compiler-libs} "$compilerLibsDir"
    chmod -R u+w "$compilerLibsDir"
  '';
})
