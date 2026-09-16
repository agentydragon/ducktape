# Intel's prebuilt NPU "Compiler-In-Plugin" libraries for OpenVINO's intel_npu
# plugin. Upstream openvino (src/plugins/intel_npu/cmake/download_compiler_libs.cmake)
# downloads this same pinned, checksummed archive mid-build when
# ENABLE_INTEL_NPU_COMPILER is set; fetching it here as an ordinary hash-pinned
# Nix derivation instead keeps the openvino build hermetic. The archive's own
# NPU_PLUGIN_COMPILER_ROOT override (same cmake file) is how a caller points
# openvino's build at this output instead of letting it fetch its own copy.
#
# Version/checksum/URL must move together with whatever openvino version
# consumes them -- see PLUGIN_COMPILER_VERSION_* and
# PLUGIN_COMPILER_UBUNTU_24_04_CHECKSUM in the cmake file above, at the
# revision matching that openvino release.
{
  lib,
  stdenvNoCC,
  fetchurl,
}:
stdenvNoCC.mkDerivation {
  pname = "intel-npu-compiler-libs";
  version = "8.2.0-9802763";

  src = fetchurl {
    url = "https://storage.openvinotoolkit.org/dependencies/thirdparty/linux/npu_compiler/npu_compiler_vcl_ubuntu_24_04-8_2_0-9802763.tar.gz";
    sha256 = "c5f2b66dee6c424cd2d8dd50cf99586d21000703121bbfc5ed9911af56221e49";
  };

  # Prebuilt binaries; just stage the archive's own layout (build_manifest.json,
  # lib/*.so, ...) at $out so it matches what NPU_PLUGIN_COMPILER_ROOT expects.
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    cp -r ./* "$out/"
    runHook postInstall
  '';

  meta = {
    description = "Prebuilt Intel NPU compiler libraries for OpenVINO's intel_npu plugin (Compiler-In-Plugin)";
    homepage = "https://github.com/openvinotoolkit/openvino/tree/2026.3.1/src/plugins/intel_npu";
    license = lib.licenses.unfree;
    platforms = [ "x86_64-linux" ];
  };
}
