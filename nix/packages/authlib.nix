# The pinned nixpkgs revision carries 1.7.2; the MCP and Plaid Spend closures
# need the same 1.9.5 security fixes as the Bazel-built environment.
{
  lib,
  fetchurl,
  python3Packages,
}:
python3Packages.buildPythonPackage {
  pname = "authlib";
  version = "1.9.5";
  pyproject = true;

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/04/96/6e7ed72bb49762a9b65d51c4cb235d793f0485cd8d6f7e9404ce955a4808/authlib-1.9.5.tar.gz";
    hash = "sha256-PwMdXptkhgiekej38baPNidfMVHGZGDWUPyQ1CBUX9c=";
  };

  build-system = [ python3Packages.setuptools ];

  dependencies = with python3Packages; [
    cryptography
    joserfc
  ];

  doCheck = false;

  pythonImportsCheck = [ "authlib" ];

  meta = {
    description = "Library for building OAuth and OpenID Connect servers";
    homepage = "https://github.com/lepture/authlib";
    license = lib.licenses.bsd3;
  };
}
