# compact-json: A JSON formatter that produces compact but human-readable output
# Not in nixpkgs, packaged here for git-commit-ai dependency
{
  lib,
  python314Packages,
  fetchPypi,
}:
python314Packages.buildPythonPackage rec {
  pname = "compact-json";
  version = "1.8.2";
  pyproject = true;

  src = fetchPypi {
    pname = "compact_json";
    inherit version;
    hash = "sha256-3CABSGlb4EuRrEXNPNpTGqAX+yVgm2a5lhoAjbJernc=";
  };

  build-system = [ python314Packages.poetry-core ];

  dependencies = with python314Packages; [
    wcwidth
    importlib-resources
    setuptools
  ];

  # No tests in PyPI distribution
  doCheck = false;

  pythonImportsCheck = [ "compact_json" ];

  meta = {
    description = "A JSON formatter that produces compact but human-readable output";
    homepage = "https://github.com/masaccio/compact-json";
    license = lib.licenses.mit;
  };
}
