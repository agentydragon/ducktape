# idna: bumped past the pinned nixpkgs revision's 3.15 to satisfy httpx2's
# runtime floor (idna>=3.18) -- nixpkgs' own pythonRuntimeDepsCheckHook enforces
# wheel-declared version floors and fails the build otherwise.
{
  lib,
  python3Packages,
  fetchurl,
}:
python3Packages.buildPythonPackage rec {
  pname = "idna";
  version = "3.20";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/58/a2/bb081bab032533a855d44de1d56f8e8426114ff1ba5d1f07a438a0a654f8/idna-3.20-py3-none-any.whl";
    hash = "sha256-q3rnEil0VTNw8L25GeGpYLLNG8HvAnZBbYltuBwUWCw=";
  };

  doCheck = false;

  pythonImportsCheck = [ "idna" ];

  meta = {
    description = "Support for the Internationalised Domain Names in Applications (IDNA) protocol";
    homepage = "https://github.com/kjd/idna";
    license = lib.licenses.bsd3;
  };
}
