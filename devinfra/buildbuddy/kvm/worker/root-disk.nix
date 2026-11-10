# The root partition is last in the pinned image. Its cloud-init growpart and resizefs modules
# expand the partition and ext4 filesystem on the worker's first boot after qemu-img enlarges it.
{
  pkgs,
  sourceImage,
}:
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
  ''
