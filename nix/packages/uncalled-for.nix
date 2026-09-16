# Required by fastmcp-slim's `server` extra (`fastmcp.server.dependencies` imports
# `CycleError`, added in 0.4.0). Stable nixpkgs ships 0.3.2; requirements_bazel.txt
# already pins 0.4.0 for the Bazel-built environment -- this override brings the Nix
# closure in line with it. A pure wheel with no dependencies, so no source build needed.
{
  lib,
  python314Packages,
  fetchurl,
}:
python314Packages.buildPythonPackage {
  pname = "uncalled-for";
  version = "0.4.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/a2/40/97cec87c077eb3291fc7905e6633e08b7ca593c57d30238444bcb6bb3d53/uncalled_for-0.4.0-py3-none-any.whl";
    hash = "sha256-FsS7MzdTLkvVVprcGSKFl2861TBUAiVtNMZ6ErXJaL0=";
  };

  doCheck = false;

  pythonImportsCheck = [ "uncalled_for" ];

  meta = {
    description = "Cycle detection and call-graph utilities used by fastmcp's server dependency injection";
    homepage = "https://pypi.org/project/uncalled-for/";
    license = lib.licenses.mit;
  };
}
