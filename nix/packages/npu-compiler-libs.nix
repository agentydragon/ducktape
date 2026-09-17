# Intel's prebuilt NPU "Compiler-In-Plugin" libraries for OpenVINO's intel_npu
# plugin. Upstream openvino (src/plugins/intel_npu/cmake/download_compiler_libs.cmake)
# downloads this same pinned archive mid-build when ENABLE_INTEL_NPU_COMPILER is
# set; fetching it here as an ordinary hash-pinned Nix derivation instead keeps
# the openvino build hermetic. openvino-npu.nix pre-populates the cmake script's
# expected extracted-archive path from this output instead of letting it fetch
# its own copy over the network (which Nix's build sandbox blocks anyway).
#
# Version/commit-sha/checksum must move together with whatever openvino version
# consumes them -- see PLUGIN_COMPILER_VERSION_* and PLUGIN_COMPILER_COMMIT_SHA
# in download_compiler_libs.cmake at the revision matching that openvino release
# (currently 2026.1.2: vcl 7.6.0, commit da3cc32).
{
  lib,
  stdenvNoCC,
  fetchurl,
}:
stdenvNoCC.mkDerivation {
  pname = "intel-npu-compiler-libs";
  version = "7.6.0-da3cc32";

  src = fetchurl {
    url = "https://storage.openvinotoolkit.org/dependencies/thirdparty/linux/npu_compiler_vcl_ubuntu_24_04-7_6_0-da3cc32.tar.gz";
    sha256 = "c7b6fd5798cfc256fa28e9a75f1e150b75fb34a8441a4fbc453315805073c5aa";
  };

  # The archive has no single wrapping directory -- build_manifest.json, lib/,
  # etc. sit directly at its root -- so stdenv's unpacker can't guess a
  # sourceRoot to cd into ("unpacker produced multiple directories").
  sourceRoot = ".";

  # Prebuilt binaries; just stage the archive's own layout (build_manifest.json,
  # lib/*.so, ...) at $out so it matches what download_compiler_libs.cmake's
  # extracted-archive directory expects.
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    cp -r ./* "$out/"
    runHook postInstall
  '';

  meta = {
    description = "Prebuilt Intel NPU compiler libraries for OpenVINO's intel_npu plugin (Compiler-In-Plugin)";
    homepage = "https://github.com/openvinotoolkit/openvino/tree/2026.1.2/src/plugins/intel_npu";
    license = lib.licenses.unfree;
    platforms = [ "x86_64-linux" ];
  };
}
