{ pkgs }:
let
  # The amd64 child of the Ubuntu 24.04 index previously used by the worker pool:
  # sha256:ea164c514b308286bdc179b403298efd2d8ae549af518a82a029e1bbb027f3a4.
  sourceImage = pkgs.dockerTools.pullImage {
    imageName = "quay.io/containerdisks/ubuntu";
    imageDigest = "sha256:2b5ff51129962719239c359bf721bfe99757cb49884447f1b49b2e585d9e6ddf";
    hash = "sha256-3m2kXEU3QHhI/hEajYYnb9uW+pyOTHuog6IRg0BJLSQ=";
    finalImageName = "quay.io/containerdisks/ubuntu";
    finalImageTag = "24.04";
    arch = "amd64";
  };

  # The pinned image has its root partition last; cloud-init grows it on first boot.
  disk =
    pkgs.runCommand "buildbuddy-kvm-worker-root-disk"
      {
        nativeBuildInputs = [
          pkgs.gnutar
          pkgs.jq
          pkgs.qemu_kvm
        ];
      }
      ''
        mkdir -p source layer disk "$out/disk"
        tar -xf ${sourceImage} -C source manifest.json
        layer=$(jq -er '.[0] | select((.Layers | length) == 1) | .Layers[0]' source/manifest.json)
        tar -xf ${sourceImage} -C layer "$layer"
        tar -xf "layer/$layer" -C layer
        test -s layer/disk/disk.img

        source_size=$(qemu-img info --output=json layer/disk/disk.img | jq -er '.["virtual-size"]')
        test "$source_size" -eq 3758096384
        cp layer/disk/disk.img "$out/disk/disk.qcow2"
        chmod u+w "$out/disk/disk.qcow2"
        qemu-img resize --quiet "$out/disk/disk.qcow2" 5368709120
        qemu-img check "$out/disk/disk.qcow2"
        qemu-img info --output=json "$out/disk/disk.qcow2" | jq -e '.["virtual-size"] == 5368709120'
      '';

  # Verify that cloud-init grows the root partition and ext4 filesystem on first boot.
  rootDiskCheck =
    pkgs.runCommand "buildbuddy-kvm-worker-root-disk-check"
      {
        nativeBuildInputs = [
          pkgs.cloud-utils
          pkgs.python3
          pkgs.qemu_kvm
        ];
      }
      ''
        python3 ${./root_disk_boot_check.py} ${disk}/disk/disk.qcow2 "$out" ${pkgs.OVMF.fd}
      '';
in
pkgs.dockerTools.buildImage {
  name = "git.allegedly.works/ducktape-ci/buildbuddy-kvm-worker";
  copyToRoot = disk;
  extraCommands = ''
    # Keep the boot check in this image derivation without adding its output to the OCI layer.
    test -s ${rootDiskCheck}
  '';
  uid = 107;
  gid = 107;
  config = { };
}
