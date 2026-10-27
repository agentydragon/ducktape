# Rugged: Piraeus partition behind the shrunk root

Interim step toward the laptop target layout (ESP + LUKS → one LVM VG, at the
next reinstall): shrink the root btrfs online and give the freed tail of the NVMe
to Piraeus as its own LUKS partition. Implements the "Laptop pools" entry of
[`cluster/docs/plans/piraeus_production.md`](../../../../../cluster/docs/plans/piraeus_production.md)
for rugged. Delete this file once the pool is admitted.

## Layout

Read 2026-10-27: 1.9 TB NVMe, 512-byte sectors, GPT. `p1` 953 MiB ESP at
`/boot`; `p2` (`start=1953792`, `size=3998842880`) is LUKS2 `cryptroot` (16 MiB
header) holding one btrfs, 697 GiB used.

| Layer                 | Size                              | Sectors                               |
| --------------------- | --------------------------------- | ------------------------------------- |
| btrfs                 | 1200 GiB                          | 2516582400                            |
| `cryptroot` mapping   | 1201 GiB (1 GiB slack over btrfs) | 2518679552                            |
| `p2`                  | 16 MiB header + mapping           | 2518712320                            |
| `p3` `rugged-piraeus` | rest, ~705 GiB                    | `start=2520666112`, `size=1480130560` |

Growing the pool later is cheap (shrink btrfs again, add the gap as another LUKS
PV to the VG); growing root back means emptying and deleting `p3`.

## Before

- Rugged's config deployed (`nixos-rebuild switch`): it unmounts the empty
  `@local-path-provisioner` subvolume and adds the `nofail` crypttab entry.
- Off-disk backup of `@home` and `@root` (read-only snapshots, `btrfs send` to
  an external disk or Atlas `tank`), plus `cryptsetup luksHeaderBackup
/dev/nvme0n1p2` stored off the laptop. Only AI sessions are backed up today.
- On AC power. No reboot is needed for the disk steps.

## Steps (root shell; verify each number before the next step)

```bash
swapoff /swap/swapfile   # btrfs cannot relocate chunks of an active swapfile
btrfs filesystem resize 1200G /   # relocates chunks above 1200 GiB; can take a while
btrfs filesystem show /           # devid 1 size 1200.00GiB

cryptsetup resize cryptroot --size 2518679552   # prompts for the root passphrase
blockdev --getsz /dev/mapper/cryptroot          # 2518679552

echo ',2518712320' | sfdisk -N 2 --no-reread /dev/nvme0n1
partx -u --nr 2 /dev/nvme0n1
blockdev --getsz /dev/nvme0n1p2                 # 2518712320

echo 'start=2520666112, size=1480130560, type=CA7D7CCB-63ED-4C53-861C-1742536059CC, name="rugged-piraeus"' \
  | sfdisk --append --no-reread /dev/nvme0n1
partx -a --nr 3 /dev/nvme0n1
udevadm settle && ls -l /dev/disk/by-partlabel/rugged-piraeus

install -d -m 700 /etc/keys
dd if=/dev/urandom of=/etc/keys/rugged-piraeus.key bs=64 count=1 && chmod 400 /etc/keys/rugged-piraeus.key
cryptsetup luksFormat --type luks2 --key-file /etc/keys/rugged-piraeus.key /dev/disk/by-partlabel/rugged-piraeus
cryptsetup luksAddKey --key-file /etc/keys/rugged-piraeus.key /dev/disk/by-partlabel/rugged-piraeus  # recovery passphrase
systemctl start 'systemd-cryptsetup@piraeus\x2dssd.service' && ls -l /dev/mapper/piraeus-ssd

swapon /swap/swapfile
mount -o subvolid=5 /dev/mapper/cryptroot /mnt && btrfs subvolume delete /mnt/@local-path-provisioner && umount /mnt
```

Reboot once when convenient to prove the keyfile unlock at boot
(`/dev/mapper/piraeus-ssd` exists afterwards).

## DRBD 9 (blocker for admission)

DRBD 9.2.16 (nixpkgs `nixos-26.05`) fails to build against rugged's 7.2.9
kernel (`linuxPackages_latest`, kept for the IPU7 camera): the kernel's socket
API now takes `struct sockaddr_unsized *`. `nixos-unstable`'s 9.3.3 driver
builds against it (test build on rugged, 2026-10-27). Next: let
`ducktape.piraeusHost` take that driver for rugged, confirm 9.3 peers with the
other satellites' DRBD, enable it and reboot (DRBD 9 loads only from the booted
system); check `modinfo -F version drbd` is 9.x.

## Admission (separate cluster change)

A `rugged` satellite configuration in `cluster/cdk8s/piraeus.py` with an `ssd`
pool on `/dev/mapper/piraeus-ssd`, tolerations for the `roaming` taint on the
satellite, CSI node and metrics Pods, then the `piraeus-ready` label. Per the
production plan, test suspend/disconnect first. Only `piraeus-ssd-local` claims
land here: the replicated class is pinned to `hil-ovh`.
