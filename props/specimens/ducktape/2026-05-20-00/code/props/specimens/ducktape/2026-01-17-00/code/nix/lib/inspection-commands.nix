# Single Source of Truth for system inspection commands
#
# Used by:
#   - nix/nixos/modules/system-inspection-sudo.nix (passwordless sudo)
#   - nix/home/claude-code/inspection-permissions.nix (Claude Code permissions)
#
# The command lists below must be kept in sync with:
#   ansible/roles/system_inspection_nopasswd/defaults/main.yml
{
  # Commands that don't need sudo (user-accessible)
  # These are only used for Claude Code permissions, not sudo rules
  noSudoCommands = [
    # Hardware information (user-accessible)
    "lspci"
    "lsusb"
    "lscpu"
    "lsblk"
    "sensors"
    # Process information
    "ps"
    "pstree"
    "top"
    "htop"
    "pgrep"
    # Memory information
    "free"
    "vmstat"
    # Disk information
    "df"
    "du"
    "findmnt"
    # Network information
    "netstat"
    "ss"
    "dig"
    "nslookup"
    "host"
    "traceroute"
    "mtr"
    "nmap"
    "lsmod"
    # Kernel/system logs (no sudo needed if systemd-journal group + dmesg_restrict=0)
    "dmesg"
    "journalctl"
    # Security/user information
    "last"
    "w"
    "who"
    "users"
    "id"
    "groups"
  ];

  # Commands needing sudo - any arguments allowed unconditionally
  # Only include commands where ALL possible flags/arguments are safe (read-only)
  sudoAnyArgsCommands = [
    # Hardware information (needs sudo for full access)
    "lshw"
    "dmidecode"
    "hwinfo"
    "biosdecode"
    "ownership"
    "vpddecode"
    "inxi"
    "acpi"
    "ipmi-sensors"
    # System information
    "uname"
    # Process information (sudo for all processes)
    "iotop"
    "pidstat"
    # Memory information
    "slabtop"
    # Disk information
    "blkid"
    # File system information - display commands only
    "lvdisplay"
    "vgdisplay"
    "pvdisplay"
    # Kernel information
    "modinfo"
    # NOTE: dmesg and journalctl removed from sudo rules - on NixOS, grant access via:
    #   users.users.${username}.extraGroups = ["systemd-journal"];
    #   boot.kernel.sysctl."kernel.dmesg_restrict" = 0;
    # This avoids passwordless sudo for commands with destructive flags.
    # Security information
    "aa-status"
    "sestatus"
    # Performance monitoring
    "iostat"
    "mpstat"
    "sar"
  ];

  # Commands needing sudo - specific subcommands only (exact match)
  # Format: { cmd = "command"; args = ["arg1" "arg2" ...]; }
  # Use "" for commands that must be run with NO arguments
  sudoExactSubcommands = [
    # GPU information - ONLY query subcommands
    {
      cmd = "nvidia-smi";
      args = ["" "-q" "-L" "pmon" "dmon"];
    }
    # ACPI information - ONLY query flags (not -W which modifies wakeup)
    {
      cmd = "acpitool";
      args = ["" "-B" "-a" "-t" "-f" "-e"];
    }
    # Last login information - ONLY no-args (avoids -C clear flag)
    {
      cmd = "lastlog";
      args = [""];
    }
    # System information - ONLY read-only subcommands
    {
      cmd = "hostnamectl";
      args = ["status"];
    }
    {
      cmd = "timedatectl";
      args = ["status" "show" "timesync-status"];
    }
    {
      cmd = "localectl";
      args = ["status"];
    }
    {
      cmd = "loginctl";
      args = ["list-sessions" "list-users"];
    }
    {
      cmd = "bootctl";
      args = ["status" "list"];
    }
    # Firmware - ONLY query subcommands
    {
      cmd = "fwupdmgr";
      args = ["get-devices" "get-updates" "get-history" "get-plugins" "security"];
    }
    # IPMI - ONLY read-only subcommands
    {
      cmd = "ipmitool";
      args = ["sensor list" "sdr list" "fru print" "mc info" "lan print" "chassis status"];
    }
    # Disk partitioning - ONLY read-only list modes
    {
      cmd = "fdisk";
      args = ["-l"];
    }
    {
      cmd = "parted";
      args = ["-l"];
    }
    # NVMe info - ONLY read operations
    {
      cmd = "nvme";
      args = ["list"];
    }
    # Network information - ONLY show/list operations
    {
      cmd = "ip";
      args = ["addr show" "-s addr show" "route show" "-s route show" "link show" "-s link show" "neighbor show" "netns list"];
    }
    # Service information - ONLY safe subcommands
    {
      cmd = "systemctl";
      args = ["list-units" "list-unit-files" "list-timers" "list-sockets"];
    }
    # File systems - ONLY read commands
    {
      cmd = "zfs";
      args = ["list"];
    }
    {
      cmd = "zpool";
      args = ["status" "list"];
    }
    {
      cmd = "btrfs";
      args = ["filesystem show" "device stats"];
    }
    # Package managers - ONLY list modes
    {
      cmd = "apt";
      args = ["list"];
    }
    {
      cmd = "dpkg";
      args = ["-l"];
    }
    {
      cmd = "snap";
      args = ["list"];
    }
    {
      cmd = "flatpak";
      args = ["list"];
    }
    # System control - ONLY read modes
    {
      cmd = "sysctl";
      args = ["-a" "-N"];
    }
    # Firewall - ONLY list/show modes
    {
      cmd = "firewall-cmd";
      args = ["--list-all"];
    }
    {
      cmd = "iptables";
      args = ["-L" "-S"];
    }
    {
      cmd = "ip6tables";
      args = ["-L" "-S"];
    }
    {
      cmd = "nft";
      args = ["list ruleset"];
    }
    # Container/VM - ONLY read-only info
    {
      cmd = "docker";
      args = ["ps" "images" "info" "version"];
    }
    {
      cmd = "podman";
      args = ["ps" "images"];
    }
    {
      cmd = "virsh";
      args = ["list"];
    }
    {
      cmd = "qm";
      args = ["list"];
    }
  ];

  # Commands needing sudo - specific subcommands with variable trailing args
  # Format: { cmd = "command"; prefixes = ["prefix1" "prefix2" ...]; }
  # These need wildcard matching for the trailing argument
  sudoWildcardSubcommands = [
    # Session/user info - needs session/user ID
    {
      cmd = "loginctl";
      prefixes = ["show-session" "show-user" "session-status" "user-status"];
    }
    # SMART disk info - needs device path
    {
      cmd = "smartctl";
      prefixes = ["-a" "-H" "-i" "-l"];
    }
    # NVMe info - needs device path
    {
      cmd = "nvme";
      prefixes = ["smart-log" "id-ctrl" "id-ns"];
    }
    # Service status - needs service name
    {
      cmd = "systemctl";
      prefixes = ["status" "show"];
    }
    # System control - read specific variable
    {
      cmd = "sysctl";
      prefixes = ["-n"];
    }
    # Proxmox - needs path argument
    {
      cmd = "pvesh";
      prefixes = ["get"];
    }
    # Performance monitoring - needs args
    {
      cmd = "perf";
      prefixes = ["stat" "top"];
    }
  ];

  # Special read-only commands for log viewing
  # These have path restrictions that Claude Code cannot express
  # Used for NixOS sudo rules, but NOT for Claude Code permissions
  logViewingCommands = [
    {
      cmd = "tail";
      args = ["-f /var/log/*"];
    }
    {
      cmd = "head";
      args = ["/var/log/*"];
    }
    {
      cmd = "cat";
      args = ["/var/log/*"];
    }
    {
      cmd = "less";
      args = ["/var/log/*"];
    }
    {
      cmd = "zcat";
      args = ["/var/log/*.gz"];
    }
    {
      cmd = "bzcat";
      args = ["/var/log/*.bz2"];
    }
  ];
}
