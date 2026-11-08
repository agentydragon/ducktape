# FastMCP 4.x is split into a dependency-bearing implementation distribution
# (`fastmcp-slim`) and a root metapackage (`fastmcp`), each published as its own wheel
# from the same PyPI release -- fetching both at the same pinned version keeps them
# from drifting without needing a from-source build.
{
  lib,
  python314Packages,
  griffelib,
  py-key-value-aio,
  uncalled-for,
  fetchurl,
}:
let
  version = "4.1.6";

  fastmcp-slim = python314Packages.buildPythonPackage {
    pname = "fastmcp-slim";
    inherit version;
    format = "wheel";

    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/0e/eb/708b5816058f72970f7ec79d3e6847ceda0f44ab38a7430cd9216ca25dd1/fastmcp_slim-4.1.6-py3-none-any.whl";
      hash = "sha256-0laRrnCn8rnYGX1iYb3Q9Ln/MwZAh801wgU5sLSLsco=";
    };

    dependencies =
      (with python314Packages; [
        # Base dependencies.
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
        certifi
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

  fastmcp = python314Packages.buildPythonPackage {
    pname = "fastmcp";
    inherit version;
    format = "wheel";

    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/ed/66/c9281ada4033818d800870dbbd865a629ee871cdf0b8e74ffc598027689a/fastmcp-4.1.6-py3-none-any.whl";
      hash = "sha256-9UNn/co9GjfFMWTr+GT7JGGs3ZKp2d9zGiRAeMAzKSY=";
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
