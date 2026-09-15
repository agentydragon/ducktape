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
  version = "3.19";
  format = "wheel";

  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/57/b0/0e52c878c53f245edd3a11020f20979b3f490f245af532c7cae3027754b5/idna-3.19-py3-none-any.whl";
    hash = "sha256-gV5756eAbVSrtYbclDrdx56LLuFpFQWWWMvv9LG0O/Q=";
  };

  doCheck = false;

  pythonImportsCheck = [ "idna" ];

  meta = {
    description = "Support for the Internationalised Domain Names in Applications (IDNA) protocol";
    homepage = "https://github.com/kjd/idna";
    license = lib.licenses.bsd3;
  };
}
