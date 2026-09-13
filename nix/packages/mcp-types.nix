# mcp-types: pydantic type definitions split out of the mcp-sdk in its v2 rework
# (mcp itself and fastmcp-slim both depend on it directly).
# Not in nixpkgs as of the pinned nixpkgs revision (introduced alongside mcp-sdk v2).
{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "mcp-types";
  version = "2.2.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/8f/d7/6ffba5d8cd5dd9b8a19478875c50e04945314ba5074e84d749283f27f62d/mcp_types-2.2.0-py3-none-any.whl";
    hash = "sha256-6kdrc+6GcJq1q8lFI4XtNswFkH5YI1ViLilFlcmgTxM=";
  };

  dependencies = with python3Packages; [
    pydantic
    typing-extensions
  ];

  doCheck = false;

  pythonImportsCheck = [ "mcp_types" ];

  meta = {
    description = "Pydantic type definitions for the Model Context Protocol SDK";
    homepage = "https://github.com/modelcontextprotocol/python-sdk";
    license = lib.licenses.mit;
  };
}
