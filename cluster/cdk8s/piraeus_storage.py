"""StorageClass names shared by the Piraeus Helm release and its PVC consumers."""

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
