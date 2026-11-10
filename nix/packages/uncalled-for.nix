# Required by fastmcp-slim's `server` extra (`fastmcp.server.dependencies` imports
# `CycleError`, added in 0.4.0). Stable nixpkgs ships 0.3.2; requirements_bazel.txt
# pins 0.6.0 for the Bazel-built environment, keeping both closures aligned.
# A pure wheel with no dependencies, so no source build is needed.
{
  lib,
  python314Packages,
  fetchurl,
}:
python314Packages.buildPythonPackage {
  pname = "uncalled-for";
  version = "0.6.0";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/a6/a7/d819bc9c5ea3d0ccbedc9999e36efebe9dbae61b024f494afc423483909a/uncalled_for-0.6.0-py3-none-any.whl";
    hash = "sha256-XGkyg2llsOawqA+ZYjiJIvO7I1Qx2PXT/twq0HtYEYk=";
  };

  doCheck = false;

  pythonImportsCheck = [ "uncalled_for" ];

  meta = {
    description = "Cycle detection and call-graph utilities used by fastmcp's server dependency injection";
    homepage = "https://pypi.org/project/uncalled-for/";
    license = lib.licenses.mit;
  };
}
