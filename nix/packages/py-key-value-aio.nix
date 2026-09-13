# Required by fastmcp 3.x (with extras filetree, keyring, memory).
# Stable nixpkgs 26.05 ships 0.3.0; this override supplies the >=0.4.4
# version required by FastMCP 3.4.x.
{
  lib,
  python3Packages,
}:
python3Packages.buildPythonPackage rec {
  pname = "py-key-value-aio";
  version = "0.4.5";
  pyproject = true;

  src = python3Packages.fetchPypi {
    pname = "py_key_value_aio";
    inherit version;
    hash = "sha256-xlY6LGq+XaXiD0+eh1wqm0JaIkSlT62/Rs8UCp7qRdc=";
  };

  # Stable nixpkgs 26.05 provides uv-build 0.10.0, while this release asks for
  # uv-build >=0.11.4. The build backend interface is unchanged here, so keep
  # the stable package set and relax only the lower bound.
  postPatch = ''
    substituteInPlace pyproject.toml \
      --replace-fail '"uv_build>=0.11.4,<0.12"' '"uv_build>=0.10.0"'
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
