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
  version = "2.1.1";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/71/d0/242e63c510f4a17381f55b1549a3f94f5687a0595984febd2b6f87a687a0/mcp_types-2.1.1-py3-none-any.whl";
    hash = "sha256-Jvn38D8qVzBxeluY4qt+tkCsNS0FoAzcclwxGGR3gpU=";
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
