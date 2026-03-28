# Single Source of Truth for system inspection commands
#
# Used by:
#   - nix/nixos/modules/system-inspection-sudo.nix (passwordless sudo)
#   - nix/home/claude_code/default.nix (Claude Code permissions)
#   - nix/home/gemini_cli.nix (Gemini CLI policies)
#
# The command lists below must be kept in sync with:
#   ansible/roles/system_inspection_nopasswd/defaults/main.yml
#
# Exports:
#   exports.sudoDetailed - Detailed format for sudo module: { type, cmd, prefix?, args? }
#   exports.noSudo - Simple format for Claude/Gemini: { type, cmd }
#   exports.sudo - Simple format for Claude/Gemini: { type, cmd = "sudo ..." }
{ lib }:
let
  # ============================================================================
  # Internal Structured Format
  # ============================================================================
  # Format: { type = "prefix"|"exact"; cmd = "command"; args? = "subcommand/args"; }
  #   - type = "prefix": allows trailing arguments (cmd args *)
  #   - type = "exact": no additional arguments (cmd args)
  #   - args is optional (omit for base command only)

  # Commands that don't need sudo (user-accessible)
  noSudoCommands = [
    # Hardware information (user-accessible)
    {
      type = "prefix";
      cmd = "lspci";
    }
    {
      type = "prefix";
      cmd = "lsusb";
    }
    {
      type = "prefix";
      cmd = "lscpu";
    }
    {
      type = "prefix";
      cmd = "lsblk";
    }
    {
      type = "prefix";
      cmd = "sensors";
    }
    # Process information
    {
      type = "prefix";
      cmd = "ps";
    }
    {
      type = "prefix";
      cmd = "pstree";
    }
    {
      type = "prefix";
      cmd = "top";
    }
    {
      type = "prefix";
      cmd = "htop";
    }
    {
      type = "prefix";
      cmd = "pgrep";
    }
    # Memory information
    {
      type = "prefix";
      cmd = "free";
    }
    {
      type = "prefix";
      cmd = "vmstat";
    }
    # Disk information
    {
      type = "prefix";
      cmd = "df";
    }
    {
      type = "prefix";
      cmd = "du";
    }
    {
      type = "prefix";
      cmd = "findmnt";
    }
    # Network information
    {
      type = "prefix";
      cmd = "netstat";
    }
    {
      type = "prefix";
      cmd = "ss";
    }
    {
      type = "prefix";
      cmd = "dig";
    }
    {
      type = "prefix";
      cmd = "nslookup";
    }
    {
      type = "prefix";
      cmd = "host";
    }
    {
      type = "prefix";
      cmd = "traceroute";
    }
    {
      type = "prefix";
      cmd = "mtr";
    }
    {
      type = "prefix";
      cmd = "nmap";
    }
    {
      type = "prefix";
      cmd = "lsmod";
    }
    # Kernel/system logs (no sudo needed if systemd-journal group + dmesg_restrict=0)
    {
      type = "prefix";
      cmd = "dmesg";
    }
    {
      type = "prefix";
      cmd = "journalctl";
    }
    # Security/user information
    {
      type = "prefix";
      cmd = "last";
    }
    {
      type = "prefix";
      cmd = "w";
    }
    {
      type = "prefix";
      cmd = "who";
    }
    {
      type = "prefix";
      cmd = "users";
    }
    {
      type = "prefix";
      cmd = "id";
    }
    {
      type = "prefix";
      cmd = "groups";
    }
  ];

  # Commands needing sudo
  # Only include commands where ALL possible flags/arguments are safe (read-only)
  sudoCommands = [
    # Hardware information - any arguments safe (prefix match)
    {
      type = "prefix";
      cmd = "lshw";
    }
    {
      type = "prefix";
      cmd = "dmidecode";
    }
    {
      type = "prefix";
      cmd = "hwinfo";
    }
    {
      type = "prefix";
      cmd = "biosdecode";
    }
    {
      type = "prefix";
      cmd = "ownership";
    }
    {
      type = "prefix";
      cmd = "vpddecode";
    }
    {
      type = "prefix";
      cmd = "inxi";
    }
    {
      type = "prefix";
      cmd = "acpi";
    }
    {
      type = "prefix";
      cmd = "ipmi-sensors";
    }
    # System information
    {
      type = "prefix";
      cmd = "uname";
    }
    # Process information
    {
      type = "prefix";
      cmd = "iotop";
    }
    {
      type = "prefix";
      cmd = "pidstat";
    }
    # Memory information
    {
      type = "prefix";
      cmd = "slabtop";
    }
    # Disk information
    {
      type = "prefix";
      cmd = "blkid";
    }
    # File system information - display commands only
    {
      type = "prefix";
      cmd = "lvdisplay";
    }
    {
      type = "prefix";
      cmd = "vgdisplay";
    }
    {
      type = "prefix";
      cmd = "pvdisplay";
    }
    # Kernel information
    {
      type = "prefix";
      cmd = "modinfo";
    }
    # NOTE: dmesg and journalctl removed from sudo rules - on NixOS, grant access via:
    #   users.users.${username}.extraGroups = ["systemd-journal"];
    #   boot.kernel.sysctl."kernel.dmesg_restrict" = 0;
    # Security information
    {
      type = "prefix";
      cmd = "aa-status";
    }
    {
      type = "prefix";
      cmd = "sestatus";
    }
    # Performance monitoring
    {
      type = "prefix";
      cmd = "iostat";
    }
    {
      type = "prefix";
      cmd = "mpstat";
    }
    {
      type = "prefix";
      cmd = "sar";
    }

    # GPU information - ONLY query subcommands (exact match)
    {
      type = "exact";
      cmd = "nvidia-smi";
    } # No args
    {
      type = "exact";
      cmd = "nvidia-smi";
      args = "-q";
    }
    {
      type = "exact";
      cmd = "nvidia-smi";
      args = "-L";
    }
    {
      type = "exact";
      cmd = "nvidia-smi";
      args = "pmon";
    }
    {
      type = "exact";
      cmd = "nvidia-smi";
      args = "dmon";
    }

    # ACPI information - ONLY query flags
    {
      type = "exact";
      cmd = "acpitool";
    }
    {
      type = "exact";
      cmd = "acpitool";
      args = "-B";
    }
    {
      type = "exact";
      cmd = "acpitool";
      args = "-a";
    }
    {
      type = "exact";
      cmd = "acpitool";
      args = "-t";
    }
    {
      type = "exact";
      cmd = "acpitool";
      args = "-f";
    }
    {
      type = "exact";
      cmd = "acpitool";
      args = "-e";
    }

    # Last login information
    {
      type = "exact";
      cmd = "lastlog";
    }

    # System information - read-only subcommands
    {
      type = "exact";
      cmd = "hostnamectl";
      args = "status";
    }
    {
      type = "exact";
      cmd = "timedatectl";
      args = "status";
    }
    {
      type = "exact";
      cmd = "timedatectl";
      args = "show";
    }
    {
      type = "exact";
      cmd = "timedatectl";
      args = "timesync-status";
    }
    {
      type = "exact";
      cmd = "localectl";
      args = "status";
    }
    {
      type = "exact";
      cmd = "loginctl";
      args = "list-sessions";
    }
    {
      type = "exact";
      cmd = "loginctl";
      args = "list-users";
    }
    {
      type = "exact";
      cmd = "bootctl";
      args = "status";
    }
    {
      type = "exact";
      cmd = "bootctl";
      args = "list";
    }

    # Firmware - query subcommands
    {
      type = "exact";
      cmd = "fwupdmgr";
      args = "get-devices";
    }
    {
      type = "exact";
      cmd = "fwupdmgr";
      args = "get-updates";
    }
    {
      type = "exact";
      cmd = "fwupdmgr";
      args = "get-history";
    }
    {
      type = "exact";
      cmd = "fwupdmgr";
      args = "get-plugins";
    }
    {
      type = "exact";
      cmd = "fwupdmgr";
      args = "security";
    }

    # IPMI - read-only subcommands
    {
      type = "exact";
      cmd = "ipmitool";
      args = "sensor list";
    }
    {
      type = "exact";
      cmd = "ipmitool";
      args = "sdr list";
    }
    {
      type = "exact";
      cmd = "ipmitool";
      args = "fru print";
    }
    {
      type = "exact";
      cmd = "ipmitool";
      args = "mc info";
    }
    {
      type = "exact";
      cmd = "ipmitool";
      args = "lan print";
    }
    {
      type = "exact";
      cmd = "ipmitool";
      args = "chassis status";
    }

    # Disk partitioning - read-only list modes
    {
      type = "exact";
      cmd = "fdisk";
      args = "-l";
    }
    {
      type = "exact";
      cmd = "parted";
      args = "-l";
    }

    # NVMe info - read operations (exact and prefix matches)
    {
      type = "exact";
      cmd = "nvme";
      args = "list";
    }
    {
      type = "prefix";
      cmd = "nvme";
      args = "smart-log";
    }
    {
      type = "prefix";
      cmd = "nvme";
      args = "id-ctrl";
    }
    {
      type = "prefix";
      cmd = "nvme";
      args = "id-ns";
    }

    # WireGuard - show tunnel status (prefix to allow interface name)
    {
      type = "prefix";
      cmd = "wg";
      args = "show";
    }

    # Network information - show/list operations
    {
      type = "exact";
      cmd = "ip";
      args = "addr show";
    }
    {
      type = "exact";
      cmd = "ip";
      args = "-s addr show";
    }
    {
      type = "exact";
      cmd = "ip";
      args = "route show";
    }
    {
      type = "exact";
      cmd = "ip";
      args = "-s route show";
    }
    {
      type = "exact";
      cmd = "ip";
      args = "link show";
    }
    {
      type = "exact";
      cmd = "ip";
      args = "-s link show";
    }
    {
      type = "exact";
      cmd = "ip";
      args = "neighbor show";
    }
    {
      type = "exact";
      cmd = "ip";
      args = "netns list";
    }

    # Service information
    {
      type = "exact";
      cmd = "systemctl";
      args = "list-units";
    }
    {
      type = "exact";
      cmd = "systemctl";
      args = "list-unit-files";
    }
    {
      type = "exact";
      cmd = "systemctl";
      args = "list-timers";
    }
    {
      type = "exact";
      cmd = "systemctl";
      args = "list-sockets";
    }
    {
      type = "prefix";
      cmd = "systemctl";
      args = "status";
    }
    {
      type = "prefix";
      cmd = "systemctl";
      args = "show";
    }

    # Session/user info (prefix - needs session/user ID)
    {
      type = "prefix";
      cmd = "loginctl";
      args = "show-session";
    }
    {
      type = "prefix";
      cmd = "loginctl";
      args = "show-user";
    }
    {
      type = "prefix";
      cmd = "loginctl";
      args = "session-status";
    }
    {
      type = "prefix";
      cmd = "loginctl";
      args = "user-status";
    }

    # SMART disk info (prefix - needs device path)
    {
      type = "prefix";
      cmd = "smartctl";
      args = "-a";
    }
    {
      type = "prefix";
      cmd = "smartctl";
      args = "-H";
    }
    {
      type = "prefix";
      cmd = "smartctl";
      args = "-i";
    }
    {
      type = "prefix";
      cmd = "smartctl";
      args = "-l";
    }

    # File systems - read commands
    {
      type = "exact";
      cmd = "zfs";
      args = "list";
    }
    {
      type = "exact";
      cmd = "zpool";
      args = "status";
    }
    {
      type = "exact";
      cmd = "zpool";
      args = "list";
    }
    {
      type = "exact";
      cmd = "btrfs";
      args = "filesystem show";
    }
    {
      type = "exact";
      cmd = "btrfs";
      args = "device stats";
    }

    # Package managers - list modes
    {
      type = "exact";
      cmd = "apt";
      args = "list";
    }
    {
      type = "exact";
      cmd = "dpkg";
      args = "-l";
    }
    {
      type = "exact";
      cmd = "snap";
      args = "list";
    }
    {
      type = "exact";
      cmd = "flatpak";
      args = "list";
    }

    # System control - read modes
    {
      type = "exact";
      cmd = "sysctl";
      args = "-a";
    }
    {
      type = "exact";
      cmd = "sysctl";
      args = "-N";
    }
    {
      type = "prefix";
      cmd = "sysctl";
      args = "-n";
    }

    # Firewall - list/show modes
    {
      type = "exact";
      cmd = "firewall-cmd";
      args = "--list-all";
    }
    {
      type = "exact";
      cmd = "iptables";
      args = "-L";
    }
    {
      type = "exact";
      cmd = "iptables";
      args = "-S";
    }
    {
      type = "exact";
      cmd = "ip6tables";
      args = "-L";
    }
    {
      type = "exact";
      cmd = "ip6tables";
      args = "-S";
    }
    {
      type = "exact";
      cmd = "nft";
      args = "list ruleset";
    }

    # Container/VM - read-only info
    {
      type = "exact";
      cmd = "docker";
      args = "ps";
    }
    {
      type = "exact";
      cmd = "docker";
      args = "images";
    }
    {
      type = "exact";
      cmd = "docker";
      args = "info";
    }
    {
      type = "exact";
      cmd = "docker";
      args = "version";
    }
    {
      type = "exact";
      cmd = "podman";
      args = "ps";
    }
    {
      type = "exact";
      cmd = "podman";
      args = "images";
    }
    {
      type = "exact";
      cmd = "virsh";
      args = "list";
    }
    {
      type = "exact";
      cmd = "qm";
      args = "list";
    }

    # Proxmox (prefix - needs path argument)
    {
      type = "prefix";
      cmd = "pvesh";
      args = "get";
    }

    # Performance monitoring (prefix - needs args)
    {
      type = "prefix";
      cmd = "perf";
      args = "stat";
    }
    {
      type = "prefix";
      cmd = "perf";
      args = "top";
    }
  ];

  # Special log viewing commands - ONLY for sudoers, NOT for Claude Code/Gemini
  # Plain command strings with path wildcards that don't fit the structured format
  logViewingCommands = [
    "tail -f /var/log/*"
    "head /var/log/*"
    "cat /var/log/*"
    "less /var/log/*"
    "zcat /var/log/*.gz"
    "bzcat /var/log/*.bz2"
  ];

  # ============================================================================
  # Transformation Functions
  # ============================================================================

  # Stringify structured format → simple format
  # Input: { type = "prefix"|"exact"; cmd; args?; }
  # Output: { type; cmd = "full command string"; }
  stringifyCommand =
    entry:
    let
      cmdStr = if entry ? args then "${entry.cmd} ${entry.args}" else entry.cmd;
    in
    {
      inherit (entry) type;
      cmd = cmdStr;
    };

  # Add sudo prefix + stringify
  # Input: { type; cmd; args?; }
  # Output: { type; cmd = "sudo full command string"; }
  addSudoAndStringify =
    entry:
    let
      stringified = stringifyCommand entry;
    in
    {
      inherit (stringified) type;
      cmd = "sudo ${stringified.cmd}";
    };
in
{
  # ============================================================================
  # External API
  # ============================================================================

  # Multiple exports for different consumer needs:
  exports = {
    # For sudo module: detailed structured format (includes logViewingCommands)
    # Format: { type = "prefix"|"exact"; cmd; args?; }
    sudoDetailed = sudoCommands ++ logViewingCommands;

    # For Claude Code/Gemini CLI: simple stringified format (excludes logViewingCommands)
    # Format: { type = "prefix"|"exact"; cmd = "full command string"; }
    noSudo = map stringifyCommand noSudoCommands;
    sudo = map addSudoAndStringify sudoCommands;
  };
}
