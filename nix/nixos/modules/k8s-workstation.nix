{ ... }:
{
  imports = [
    ./gui.nix
    ./workstation.nix
    ./bazel
    ./system-inspection-sudo.nix
    ./k8s-worker.nix
  ];

  ducktape.systemInspectionSudo.enable = true;
  ducktape.k8sWorker.enable = true;
}
