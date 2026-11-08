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
  version = "2.3.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/ce/48/32583066b155bb5d4507e7887dc88ee23118cc4de8e2a12870c49719e2a8/mcp_types-2.3.0-py3-none-any.whl";
    hash = "sha256-lo79va7fqwatrkDTeKNDlfEJDFkh1L48nN4oOq922R0=";
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
