# pyrage: Python bindings for rage (age encryption in Rust)
# Not in nixpkgs, installed from pre-built manylinux wheel.
{
  lib,
  python3Packages,
  fetchurl,
  autoPatchelfHook,
}:
python3Packages.buildPythonPackage rec {
  pname = "pyrage";
  version = "1.3.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/38/f3/e91bf604fd40c42c60e8f95075cddb0b85d0bdf452f736b533b1bad550e0/pyrage-1.3.0-cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl";
    hash = "sha256-qwZrIpJcWg7F/q0uIeRYayHV2nMAVcfkbKqXi9md6TY=";
  };

  nativeBuildInputs = [ autoPatchelfHook ];

  # No test suite in the wheel
  doCheck = false;

  pythonImportsCheck = [ "pyrage" ];

  meta = {
    description = "Python bindings for rage (age encryption in Rust)";
    homepage = "https://github.com/woodruffw/pyrage";
    license = lib.licenses.mit;
  };
}
