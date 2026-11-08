{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "openapi-pydantic";
  version = "0.6.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/31/57/9d3eb823a03c33e9d4c810e6a3276e3a6dc820a7fdc0961db2c04e613817/openapi_pydantic-0.6.0-py3-none-any.whl";
    hash = "sha256-i9bVSKJQ2jfblvRhvNUrNGjog0rX1tsUsAprLrvRY6M=";
  };

  dependencies = [ python3Packages.pydantic ];

  doCheck = false;
  pythonImportsCheck = [ "openapi_pydantic" ];

  meta = {
    description = "OpenAPI models for Pydantic";
    homepage = "https://github.com/mike-oakley/openapi-pydantic";
    license = lib.licenses.mit;
  };
}
