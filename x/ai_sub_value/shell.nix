{ pkgs, baseShell }:
pkgs.mkShell {
  dontAddPythonPath = "1";
  inputsFrom = [ baseShell ];
  packages = [
    (pkgs.python314.withPackages (ps: [
      ps.ipykernel
      ps.jupyterlab
      ps.matplotlib
      ps.nbclient
      ps.numpy
      ps.pandas
      ps.scipy
    ]))
  ];
}
