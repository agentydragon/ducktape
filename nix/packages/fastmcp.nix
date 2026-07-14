# FastMCP 3.x is split into a dependency-bearing implementation distribution
# (`fastmcp-slim`) and a root metapackage (`fastmcp`). Build both from the same
# upstream source so their versions cannot drift.
{
  lib,
  python3Packages,
  griffelib,
  py-key-value-aio,
  uncalled-for,
  fetchFromGitHub,
}:
let
  version = "3.4.4";

  src = fetchFromGitHub {
    owner = "PrefectHQ";
    repo = "fastmcp";
    tag = "v${version}";
    hash = "sha256-aqFht99jbBIg6tFBlHeWebC0xDtind5w4+RIAdXJ50U=";
  };

  build-system = with python3Packages; [
    hatchling
    uv-dynamic-versioning
  ];

  fastmcp-slim = python3Packages.buildPythonPackage {
    pname = "fastmcp-slim";
    inherit version src build-system;
    pyproject = true;

    sourceRoot = "${src.name}/fastmcp_slim";

    # The GitHub archive has no `.git` directory for uv-dynamic-versioning to
    # inspect. Its documented bypass keeps both distributions on the tag's
    # exact version without rewriting upstream metadata.
    env.UV_DYNAMIC_VERSIONING_BYPASS = version;

    # Install the base package together with FastMCP's `client` and `server`
    # extras. The root `fastmcp` distribution depends on this complete runtime.
    dependencies =
      (with python3Packages; [
        # Base dependencies.
        email-validator
        platformdirs
        pydantic
        pydantic-settings
        python-dotenv
        rich
        typing-extensions

        # `mcp` extra shared by the client and server extras.
        exceptiongroup
        httpx
        mcp
        opentelemetry-api
        starlette

        # Client and server extras.
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

    # The full test suite needs network, optional providers (anthropic,
    # openai, gemini), and live MCP servers. Ducktape exercises its FastMCP
    # use through Bazel; these imports guard the Nix runtime closure.
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
    inherit version src build-system;
    pyproject = true;

    env.UV_DYNAMIC_VERSIONING_BYPASS = version;

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
