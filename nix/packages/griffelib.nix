# Required by fastmcp 3.2.x; not in nixpkgs 25.11.
{
  lib,
  python3Packages,
  fetchFromGitHub,
}:
python3Packages.buildPythonPackage rec {
  pname = "griffelib";
  version = "2.0.0";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "mkdocstrings";
    repo = "griffe";
    tag = version;
    hash = "sha256-SiUkgkaHtq2aWraL5BJvItOExTGUQ+e6pQVXEwTM0mk=";
  };

  sourceRoot = "${src.name}/packages/griffelib";

  build-system = with python3Packages; [
    hatchling
    pdm-backend
    uv-dynamic-versioning
  ];

  doCheck = false;

  pythonImportsCheck = [ "griffe" ];

  meta = {
    description = "Python program signature extraction library";
    homepage = "https://github.com/mkdocstrings/griffe";
    license = lib.licenses.isc;
  };
}
