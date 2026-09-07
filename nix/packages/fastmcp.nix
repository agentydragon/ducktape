# FastMCP 4.x is split into a dependency-bearing implementation distribution
# (`fastmcp-slim`) and a root metapackage (`fastmcp`), each published as its own wheel
# from the same PyPI release -- fetching both at the same pinned version keeps them
# from drifting without needing a from-source build.
{
  lib,
  python3Packages,
  griffelib,
  py-key-value-aio,
  uncalled-for,
  fetchurl,
}:
let
  version = "4.0.3";

  fastmcp-slim = python3Packages.buildPythonPackage {
    pname = "fastmcp-slim";
    inherit version;
    format = "wheel";

    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/43/4b/6b31820d87f56d5773538860878c8634b618cd1e9044b3bfbd8a78380a0f/fastmcp_slim-4.0.3-py3-none-any.whl";
      hash = "sha256-N1btS9n4L0Ctr1GXS3zdFGOra59HnPababJpKWkxK4c=";
    };

    dependencies =
      (with python3Packages; [
        email-validator
        platformdirs
        pydantic
        pydantic-settings
        python-dotenv
        rich
        typing-extensions

        exceptiongroup
        httpx2
        mcp
        mcp-types
        opentelemetry-api
        starlette

        authlib
        cyclopts
        jsonref
        jsonschema-path
        joserfc
        openapi-pydantic
        packaging
        pyperclip
        python-multipart
        pyyaml
        uvicorn
        watchfiles
        websockets
      ])
      ++ [
        griffelib
        py-key-value-aio
        uncalled-for
      ];

    doCheck = false;

    pythonImportsCheck = [
      "fastmcp"
      "fastmcp.server.auth.oidc_proxy"
      "fastmcp.server.auth.providers.jwt"
    ];

    meta = {
      description = "Dependency-slim FastMCP implementation with client and server support";
      homepage = "https://github.com/PrefectHQ/fastmcp";
      license = lib.licenses.asl20;
      mainProgram = "fastmcp";
    };
  };

  fastmcp = python3Packages.buildPythonPackage {
    pname = "fastmcp";
    inherit version;
    format = "wheel";

    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/e4/cb/66600db497b3be21cf141f01296308169859d48ed9b1bc8ba7c9d83279b7/fastmcp-4.0.3-py3-none-any.whl";
      hash = "sha256-90P0MZPh2vYG5kl6nd26W7qtffeVepj9CTcW7PRFi5k=";
    };

    dependencies = [ fastmcp-slim ];

    doCheck = false;

    pythonImportsCheck = [ "fastmcp" ];

    meta = {
      description = "Fast Pythonic way to build MCP servers and clients";
      homepage = "https://github.com/PrefectHQ/fastmcp";
      license = lib.licenses.asl20;
    };
  };
in
{
  inherit fastmcp fastmcp-slim;
}
