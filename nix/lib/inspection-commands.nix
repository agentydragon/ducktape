# Read-only system inspection commands — single source of truth
#
# This list defines commands that are safe to run without human oversight because
# they only read system state. It serves two purposes:
#
#   1. Passwordless sudo (NixOS sudoers) — so the user (and agents running as
#      the user) can run privileged read-only commands without typing a password.
#      Consumer: nix/nixos/modules/system-inspection-sudo.nix
#
#   2. AI agent auto-approval — so Claude Code / Gemini CLI can execute these
#      commands without prompting the user for confirmation.
#      Consumers: nix/home/claude_code/default.nix, nix/home/gemini_cli.nix
#
# Commands are split into two groups:
#   - noSudoCommands: don't need root. Only role (2) applies.
#   - sudoCommands: need root. Both roles apply — agents run them as
#     "sudo <cmd>" which works without a password thanks to role (1).
#
# The shared safety invariant is: every command here is read-only. If a command
# can modify system state with any flag combination, it must use mkExact to
# restrict to specific safe invocations.
#
# Must be kept in sync with:
#   ansible/roles/system_inspection_nopasswd/defaults/main.yml
{ lib }:
let
  # Constructors accept a single-token string as shorthand, but every structured
  # entry stores args as token lists. Multi-token arguments must be explicit.
  normalizeArgs =
    args:
    if builtins.isList args then
      args
    else if
      builtins.isString args
      && !(lib.any (whitespace: lib.hasInfix whitespace args) [
        " "
        "\t"
        "\n"
        "\r"
      ])
    then
      [ args ]
    else
      throw "inspection command args must be a token list; single-token strings are accepted as shorthand";

  # Helpers — prefix allows trailing args, exact does not.
  mkPrefixArgs = cmd: args: {
    type = "prefix";
    inherit cmd;
    args = normalizeArgs args;
  };
  mkPrefix = cmd: mkPrefixArgs cmd [ ];
  mkExactArgs = cmd: args: {
    type = "exact";
    inherit cmd;
    args = normalizeArgs args;
  };
  mkExact = cmd: mkExactArgs cmd [ ];

  # Batch helpers — fan out one base command to multiple arg variants.
  mkExactMulti = cmd: argsList: map (mkExactArgs cmd) argsList;
  mkPrefixMulti = cmd: argsList: map (mkPrefixArgs cmd) argsList;

  # Commands that don't need sudo (user-accessible)
  noSudoCommands = [
    # Hardware information
    (mkPrefix "lspci")
    (mkPrefix "lsusb")
    (mkPrefix "lscpu")
    (mkPrefix "lsblk")
    (mkPrefix "sensors")
    # Process information
    (mkPrefix "ps")
    (mkPrefix "pstree")
    (mkPrefix "top")
    (mkPrefix "htop")
    (mkPrefix "pgrep")
    # Memory information
    (mkPrefix "free")
    (mkPrefix "vmstat")
    # Disk information
    (mkPrefix "df")
    (mkPrefix "du")
    (mkPrefix "findmnt")
    # Network information
    (mkPrefix "netstat")
    (mkPrefix "ss")
    (mkPrefix "dig")
    (mkPrefix "nslookup")
    (mkPrefix "host")
    (mkPrefix "traceroute")
    (mkPrefix "mtr")
    (mkPrefix "nmap")
    (mkPrefix "lsmod")
    # Kernel/system logs (no sudo needed if systemd-journal group + dmesg_restrict=0)
    (mkPrefix "dmesg")
    (mkPrefix "journalctl")
    # Security/user information
    (mkPrefix "last")
    (mkPrefix "w")
    (mkPrefix "who")
    (mkPrefix "users")
    (mkPrefix "id")
    (mkPrefix "groups")
    (mkPrefix "lpstat")
    # Radio/device kill switch status
    (mkExactArgs "rfkill" "list")
  ]
  ++ (mkPrefixMulti "gsettings" [
    "get"
    "list-recursively"
    "list-schemas"
  ])
  ++ (mkPrefixMulti "dconf" [
    "dump"
    "read"
  ])
  ++ (mkPrefixMulti "gnome-extensions" [
    "list"
    "info"
    "show"
  ]);

  # Commands needing sudo
  # Only include commands where ALL possible flags/arguments are safe (read-only)
  sudoCommands = [
    # Hardware information - any arguments safe
    (mkPrefix "lshw")
    (mkPrefix "dmidecode")
    (mkPrefix "hwinfo")
    (mkPrefix "biosdecode")
    (mkPrefix "ownership")
    (mkPrefix "vpddecode")
    (mkPrefix "inxi")
    (mkPrefix "acpi")
    (mkPrefix "ipmi-sensors")
    (mkPrefix "uname")
    (mkPrefix "iotop")
    (mkPrefix "pidstat")
    # Memory information
    (mkPrefix "slabtop")
    # Disk information
    (mkPrefix "blkid")
    # LVM information - read-only display/list commands
    (mkPrefix "lvdisplay")
    (mkPrefix "vgdisplay")
    (mkPrefix "pvdisplay")
    (mkPrefix "lvs")
    (mkPrefix "vgs")
    (mkPrefix "pvs")
    # Kernel information
    (mkPrefix "modinfo")
    # NOTE: dmesg and journalctl removed from sudo rules - on NixOS, grant access via:
    #   users.users.${username}.extraGroups = ["systemd-journal"];
    #   boot.kernel.sysctl."kernel.dmesg_restrict" = 0;
    # Security information
    (mkPrefix "aa-status")
    (mkPrefix "sestatus")
    # Performance monitoring
    (mkPrefix "iostat")
    (mkPrefix "mpstat")
    (mkPrefix "sar")
    # ACPI information - ONLY query flags
    (mkExact "acpitool") # TODO: check that this is safe
    (mkExact "lastlog")
    (mkExactArgs "localectl" "status")
    (mkExactArgs "hostnamectl" "status")
    # GPU information - ONLY query subcommands
    (mkExact "nvidia-smi")
  ]
  ++ mkExactMulti "nvidia-smi" [
    "-q"
    "-L"
    "pmon"
    "dmon"
  ]
  ++ mkExactMulti "acpitool" [
    "-B"
    "-a"
    "-t"
    "-f"
    "-e"
  ]
  ++ mkExactMulti "timedatectl" [
    "status"
    "show"
    "timesync-status"
  ]
  ++ mkExactMulti "loginctl" [
    "list-sessions"
    "list-users"
  ]
  ++ mkExactMulti "bootctl" [
    "status"
    "list"
  ]
  ++ mkPrefixMulti "resolvectl" [
    "query"
    "service"
    "openpgp"
    "tlsa"
    "status"
    "statistics"
  ]
  ++ mkPrefixMulti "nmcli" [
    "-L"
    [
      "connection"
      "show"
    ]
  ]
  ++ mkExactMulti "fwupdmgr" [
    "get-devices"
    "get-updates"
    "get-history"
    "get-plugins"
    "security"
  ]

  # IPMI - read-only subcommands
  ++ mkExactMulti "ipmitool" [
    [
      "sensor"
      "list"
    ]
    [
      "sdr"
      "list"
    ]
    [
      "fru"
      "print"
    ]
    [
      "mc"
      "info"
    ]
    [
      "lan"
      "print"
    ]
    [
      "chassis"
      "status"
    ]
  ]
  ++ [
    (mkExactArgs "fdisk" "-l")
    (mkExactArgs "parted" "-l")
    (mkExactArgs "nvme" "list")
    (mkPrefixArgs "wg" "show")
    (mkPrefix "ping")
  ]
  ++ mkPrefixMulti "nvme" [
    "smart-log"
    "id-ctrl"
    "id-ns"
  ]
  ++ mkExactMulti "ip" [
    [
      "addr"
      "show"
    ]
    [
      "-s"
      "addr"
      "show"
    ]
    [
      "route"
      "show"
    ]
    [
      "-s"
      "route"
      "show"
    ]
    [
      "link"
      "show"
    ]
    [
      "-s"
      "link"
      "show"
    ]
    [
      "neighbor"
      "show"
    ]
    [
      "netns"
      "list"
    ]
  ]

  # Service information
  ++ mkPrefixMulti "systemctl" [
    "list-units"
    "list-unit-files"
    "list-timers"
    "list-sockets"
    "status"
    "show"
  ]
  ++ mkPrefixMulti "nix" [
    "build"
    "hash"
    "search"
  ]

  # Session/user info (prefix - needs session/user ID)
  ++ mkPrefixMulti "loginctl" [
    "show-session"
    "show-user"
    "session-status"
    "user-status"
  ]

  # SMART disk info (prefix - needs device path)
  ++ mkPrefixMulti "smartctl" [
    "-a"
    "-H"
    "-i"
    "-l"
  ]

  ++ [
    # File systems - read commands
    (mkExactArgs "zfs" "list")
  ]
  ++ mkExactMulti "zpool" [
    "status"
    "list"
  ]
  ++ mkExactMulti "btrfs" [
    [
      "filesystem"
      "show"
    ]
    [
      "device"
      "stats"
    ]
  ]
  ++ [

    # Package managers - list modes
    (mkExactArgs "apt" "list")
    (mkExactArgs "dpkg" "-l")
    (mkExactArgs "snap" "list")
    (mkExactArgs "flatpak" "list")

    # System control - read modes
    (mkExactArgs "sysctl" "-a")
    (mkExactArgs "sysctl" "-N")
    (mkPrefixArgs "sysctl" "-n")

    # Firewall - list/show modes
    (mkExactArgs "firewall-cmd" "--list-all")
  ]
  ++ mkExactMulti "iptables" [
    "-L"
    "-S"
  ]
  ++ mkExactMulti "ip6tables" [
    "-L"
    "-S"
  ]
  ++ [
    (mkExactArgs "nft" [
      "list"
      "ruleset"
    ])
  ]

  # Container/VM - read-only info
  ++ mkExactMulti "docker" [
    "ps"
    "images"
    "info"
    "version"
  ]
  ++ mkExactMulti "podman" [
    "ps"
    "images"
  ]
  ++ [
    (mkExactArgs "virsh" "list")
    (mkExactArgs "qm" "list")

    # Proxmox (prefix - needs path argument)
    (mkPrefixArgs "pvesh" "get")
  ]
  ++ mkPrefixMulti "perf" [
    "stat"
    "top"
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

  # Stringify structured format → simple format
  # Input: { type = "prefix"|"exact"; cmd; args = [ ... ]; }
  # Output: { type; cmd = "full command string"; }
  stringifyCommand =
    entry:
    let
      cmdStr = lib.concatStringsSep " " ([ entry.cmd ] ++ entry.args);
    in
    {
      inherit (entry) type;
      cmd = cmdStr;
    };

  # Add sudo prefix + stringify
  # Input: { type; cmd; args = [ ... ]; }
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
  # Multiple exports for different consumer needs:
  exports = {
    # For sudo module: detailed structured format (includes logViewingCommands)
    # Format: { type = "prefix"|"exact"; cmd; args = [ ... ]; }
    sudoDetailed = sudoCommands ++ logViewingCommands;

    # For Claude Code/Gemini CLI: simple stringified format (excludes logViewingCommands)
    # Format: { type = "prefix"|"exact"; cmd = "full command string"; }
    noSudo = map stringifyCommand noSudoCommands;
    sudo = map addSudoAndStringify sudoCommands;
  };
}
