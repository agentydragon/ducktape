{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "certifi";
  version = "2026.10.23";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/32/d1/a3652fe2c19134d0d15922177821d863cbd7e842d53a21bbe7e8e3a1b5ac/certifi-2026.10.23-py3-none-any.whl";
    hash = "sha256-6bVN525TC7bPDM0DZ4cQOstY/UK6kw6ov5SN26ovYdE=";
  };

  doCheck = false;
  pythonImportsCheck = [ "certifi" ];

  meta = {
    description = "Python CA certificate bundle";
    homepage = "https://github.com/certifi/python-certifi";
    license = lib.licenses.mpl20;
  };
}
