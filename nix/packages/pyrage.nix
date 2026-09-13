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
  version = "1.4.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/d9/f0/41da7d88cfdad2eba492f3275ec50a65916ccd3a78140fb15875569ad1ff/pyrage-1.4.0-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl";
    hash = "sha256-f06wy8K0ruXerJhixHs4lYZt9VVBU5Ua+CLpxYHmhVg=";
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
