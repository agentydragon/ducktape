# Required by fastmcp 3.x (with extras filetree, keyring, memory).
# Not in nixpkgs as of 25.11.
{
  lib,
  python3Packages,
}:
python3Packages.buildPythonPackage rec {
  pname = "py-key-value-aio";
  version = "0.4.4";
  pyproject = true;

  src = python3Packages.fetchPypi {
    pname = "py_key_value_aio";
    inherit version;
    hash = "sha256-4wEuYkPtfMCbsFRXvU0DsbpcKxyocACWs5J9t5/7vlU=";
  };

  # nixpkgs ships uv-build 0.9.x; the sdist pins `uv_build<0.9.0`. The build
  # backend interface is unchanged between 0.8 → 0.9, so drop the upper bound.
  postPatch = ''
    substituteInPlace pyproject.toml \
      --replace-fail '"uv_build>=0.8.2,<0.9.0"' '"uv_build>=0.8.2"'
  '';

  build-system = with python3Packages; [ uv-build ];

  dependencies = with python3Packages; [
    beartype
    typing-extensions
    # filetree extra
    aiofile
    anyio
    # keyring extra
    keyring
    # memory extra
    cachetools
    pathvalidate
  ];

  # Tests require optional backends (Redis, Postgres, etc.).
  doCheck = false;

  pythonImportsCheck = [ "key_value.aio" ];

  meta = {
    description = "Async key-value store abstraction with pluggable backends (filetree, keyring, memory used here)";
    homepage = "https://pypi.org/project/py-key-value-aio/";
    license = lib.licenses.mit;
  };
}
