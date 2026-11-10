# mcp: the Model Context Protocol Python SDK. Ducktape pins mcp[cli]>=2.3.0 (see
# requirements_bazel.txt); the nixpkgs-vendored python3Packages.mcp lags behind (1.26.0
# as of the pinned nixpkgs revision). Package the pinned version directly from the
# published wheel instead of patching the vendored derivation.
{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "mcp";
  version = "2.3.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/20/d7/4a2878f128df729db3ca04e71bfce26405cc2e0c9ebe19c592feeb4c61b9/mcp-2.3.0-py3-none-any.whl";
    hash = "sha256-3QxEwInRZFPorjGjh3oAVNeiMUyqqB9eBUG5sXNLI3c=";
  };

  dependencies = with python3Packages; [
    anyio
    httpx2
    jsonschema
    mcp-types
    opentelemetry-api
    pydantic
    pyjwt
    cryptography
    python-multipart
    sse-starlette
    starlette
    typing-extensions
    typing-inspection
    uvicorn

    # [cli] extra -- ducktape pins mcp[cli].
    python-dotenv
    typer
  ];

  doCheck = false;

  pythonImportsCheck = [
    "mcp"
    "mcp.shared.exceptions"
  ];

  meta = {
    description = "Model Context Protocol Python SDK";
    homepage = "https://github.com/modelcontextprotocol/python-sdk";
    license = lib.licenses.mit;
  };
}
