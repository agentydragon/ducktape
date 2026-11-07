# The pinned nixpkgs revision carries 1.7.2; the Plaid Spend wheel requires 1.8.0.
{
  lib,
  fetchurl,
  python3Packages,
}:
python3Packages.buildPythonPackage {
  pname = "authlib";
  version = "1.8.0";
  pyproject = true;

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/f1/51/bc1729d3cfdc214b4935f4e886e4dd443c3065fd8e1e66423fe84b490f81/authlib-1.8.0.tar.gz";
    hash = "sha256-8+zV8dpzcmL7U78aTZXE6hrZ3VCTFlh6JVyZqxg4pPA=";
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
