# Tiny utility package required by fastmcp 3.x; not in nixpkgs as of 25.11.
{
  lib,
  python3Packages,
}:
python3Packages.buildPythonPackage rec {
  pname = "uncalled-for";
  version = "0.3.2";
  pyproject = true;

  src = python3Packages.fetchPypi {
    pname = "uncalled_for";
    inherit version;
    hash = "sha256-ifXbzXHiuPR8AwsfowLmzOLseV0axWXutlJcX+VcuKI=";
  };

  # `hatch-vcs` reads the version from git tags, but the sdist has none.
  # Substitute a static version so `hatchling` can build without it.
  postPatch = ''
    substituteInPlace pyproject.toml \
      --replace-fail 'dynamic = ["version"]' 'version = "${version}"' \
      --replace-fail '"hatchling", "hatch-vcs"' '"hatchling"'
  '';

  build-system = [ python3Packages.hatchling ];

  pythonImportsCheck = [ "uncalled_for" ];

  meta = {
    description = "Helper for inspecting un-awaited coroutines";
    homepage = "https://pypi.org/project/uncalled-for/";
    license = lib.licenses.mit;
  };
}
