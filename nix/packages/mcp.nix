# mcp: the Model Context Protocol Python SDK. Ducktape pins mcp[cli]>=2.0.0 (see
# requirements_bazel.txt); the nixpkgs-vendored python3Packages.mcp lags behind (1.26.0
# as of the pinned nixpkgs revision) and lacks mcp.shared.exceptions.MCPError, which
# mcp_infra/compositor/resources_server.py imports. Package the pinned version directly
# from the published wheel instead of patching the vendored derivation.
{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "mcp";
  version = "2.1.1";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/50/af/8644cc5fa26a59afd2df2e98eeb19e72926887fa4b7441aba4ff661140db/mcp-2.1.1-py3-none-any.whl";
    hash = "sha256-HGwxxdZHHFjbdq86+K9n9G0R0B8KWQd9CjCMvbPT6RU=";
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
