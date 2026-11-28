"""The Piraeus facts shared by its Helm release and its storage consumers: StorageClass
names and the node label marking a prepared satellite node."""

# Terraform machine-config nodeLabels and nix/nixos set this on every node whose LINSTOR
# satellite and raw-device pool exist; scheduling DRBD-backed Pods selects it.
PIRAEUS_READY_LABEL = "storage.allegedly.works/piraeus-ready"

SSD_LOCAL = "piraeus-ssd-local"
HDD_LOCAL = "piraeus-hdd-local"
SSD_LOCAL_DELETE = "piraeus-ssd-local-delete"
SSD_REPLICATED = "piraeus-ssd-2-autodiskful"
HDD_REPLICATED = "piraeus-hdd-2"

# Local classes cannot follow an evicted Pod to another node. The descheduler protects all of them.
LOCAL_CLASSES = {
    SSD_LOCAL: ("ssd", "Retain"),
    # Rebuildable worker cache and work disks return their space on scale-down.
    SSD_LOCAL_DELETE: ("ssd", "Delete"),
    HDD_LOCAL: ("hdd", "Retain"),
}
