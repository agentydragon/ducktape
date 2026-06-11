# fastmcp 3.x — required by the ducktape umbrella wheel. Nixpkgs 25.11 only
# ships 2.x; this build matches the Bazel-side pin in requirements_bazel.txt.
{
  lib,
  python3Packages,
  py-key-value-aio,
  uncalled-for,
  fetchFromGitHub,
}:
python3Packages.buildPythonPackage rec {
  pname = "fastmcp";
  version = "3.1.0";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "jlowin";
    repo = "fastmcp";
    tag = "v${version}";
    hash = "sha256-MnU69JuMlRwiUFIOaws9N7Hw6oDnrIIwCoWuHFrvbHQ=";
  };

  # `uv-dynamic-versioning` reads the version from `git describe`; the source
  # tarball has no .git, so substitute a static version into the build
  # metadata to keep hatchling happy.
  postPatch = ''
    substituteInPlace pyproject.toml \
      --replace-fail 'dynamic = ["version"]' 'version = "${version}"' \
      --replace-fail 'source = "uv-dynamic-versioning"' "" \
      --replace-fail 'requires = ["hatchling", "uv-dynamic-versioning>=0.7.0"]' \
                     'requires = ["hatchling"]'
  '';

  build-system = with python3Packages; [ hatchling ];

  dependencies =
    (with python3Packages; [
      authlib
      cyclopts
      exceptiongroup
      httpx
      jsonref
      jsonschema-path
      mcp
      openapi-pydantic
      opentelemetry-api
      packaging
      platformdirs
      pydantic
      email-validator
      pyperclip
      python-dotenv
      pyyaml
      rich
      uvicorn
      watchfiles
      websockets
    ])
    ++ [
      py-key-value-aio
      uncalled-for
    ];

  # The full test suite needs network, optional providers (anthropic,
  # openai, gemini), and live MCP servers. We rely on Bazel-side tests for
  # ducktape's actual usage of fastmcp; the Nix package only needs to import.
  doCheck = false;

  pythonImportsCheck = [
    "fastmcp"
    "fastmcp.server.providers.proxy"
  ];

  meta = {
    description = "Fast Pythonic way to build MCP servers and clients";
    homepage = "https://github.com/jlowin/fastmcp";
    license = lib.licenses.asl20;
  };
}
