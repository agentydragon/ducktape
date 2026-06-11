"""
SBPL (macOS Seatbelt) typed policy models.

Layering contract:
- Pure data only. No implicit defaults beyond field defaults.
- No platform probing, path injection, or mutation helpers here.
- Compiler/validator/runner live in sibling modules.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Action(StrEnum):
    """Allow/deny action for rule clauses.

    Use uppercase member names for clarity in policies, while values remain
    the lowercase SBPL strings for correct rendering.
    """

    ALLOW = "allow"
    DENY = "deny"


class FileOp(StrEnum):
    """SBPL file operation kinds (string-valued for textual rendering)."""

    FILE_READ_STAR = "file-read*"
    FILE_WRITE_STAR = "file-write*"
    FILE_READ_METADATA = "file-read-metadata"
    FILE_MAP_EXECUTABLE = "file-map-executable"


class NetworkOp(StrEnum):
    """SBPL network operation kinds (string-valued for textual rendering)."""

    NETWORK_INBOUND = "network-inbound"
    NETWORK_OUTBOUND = "network-outbound"
    NETWORK_BIND = "network-bind"


class DefaultBehavior(StrEnum):
    """Default SBPL behavior for unspecified operations."""

    DENY = "deny"
    ALLOW = "allow"


class Subpath(BaseModel):
    """Filter representing an SBPL (subpath "<dir>") predicate."""

    subpath: str

    model_config = ConfigDict(extra="forbid")


class LiteralFilter(BaseModel):
    """Filter representing an SBPL (literal "<path>") predicate."""

    literal: str

    model_config = ConfigDict(extra="forbid")


# Tagged union used by FileRule.filters
PathFilter = Subpath | LiteralFilter


class FileRule(BaseModel):
    """
    File operation rule. Each filter produces a separate SBPL clause.

    Example SBPL render for allow+file-read*+subpath("/usr/lib"):
      (allow file-read* (subpath "/usr/lib"))
    """

    action: Action = Action.ALLOW
    op: FileOp
    filters: list[PathFilter] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class MachLookupRule(BaseModel):
    """
    Mach lookup permissions by global service names.

    action applies to all names in the list.
    """

    action: Action = Action.ALLOW
    global_names: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class NetworkRule(BaseModel):
    """
    Network permission rule.

    local_only=True renders the (local ip) predicate.
    """

    action: Action = Action.ALLOW
    op: NetworkOp
    local_only: bool = False

    model_config = ConfigDict(extra="forbid")


class SystemRule(BaseModel):
    """
    System-level toggles. True => emit an allow clause in compiler.
    """

    system_socket: bool = False
    # If True, allows unrestricted sysctl-read. If False, names/prefixes (when
    # provided) restrict the allowance to specific sysctl keys.
    sysctl_read: bool = False
    # Optional fine-grained filters for sysctl-read
    # Example SBPL:
    # (allow sysctl-read (sysctl-name "hw.ncpu") (sysctl-name-prefix "kern.proc.pid."))
    sysctl_names: list[str] = Field(default_factory=list)
    sysctl_prefixes: list[str] = Field(default_factory=list)
    # Other system toggles
    user_preference_read: bool = False
    ipc_posix_sem: bool = False

    model_config = ConfigDict(extra="forbid")


class ProcessRule(BaseModel):
    """
    Process/signal primitives.
    """

    allow_process_star: bool = True
    # Signal permissions
    allow_signal_self: bool = True
    allow_signal_same_sandbox: bool = False
    # Process management
    allow_process_exec: bool = False
    allow_process_fork: bool = False
    # Process info
    allow_process_info_same_sandbox: bool = False

    model_config = ConfigDict(extra="forbid")


class TraceConfig(BaseModel):
    """
    Seatbelt trace configuration.

    If enabled and path is provided, compiler will emit (trace "<path>").
    """

    enabled: bool = False
    path: str | None = None

    model_config = ConfigDict(extra="forbid")


class EnvPassthroughMode(StrEnum):
    """How to construct the child process environment."""

    WHITELIST = "whitelist"
    ALL = "all"


class EnvConfig(BaseModel):
    """Environment pass-through configuration.

    - mode: 'whitelist' (default) passes through only selected variables from the
      current process environment; 'all' passes through the full environment.
    - whitelist: variable names to include when mode is 'whitelist'.
      The executor may apply a conservative built-in default when empty.
    """

    mode: EnvPassthroughMode = EnvPassthroughMode.WHITELIST
    whitelist: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class SBPLPolicy(BaseModel):
    """
    Top-level SBPL policy model (useful subset).

    default_behavior controls the header (allow/deny default).
    Lists preserve caller-provided order.
    """

    version: int = 1
    default_behavior: DefaultBehavior = DefaultBehavior.DENY

    process: ProcessRule = Field(default_factory=ProcessRule)
    files: list[FileRule] = Field(default_factory=list)
    network: list[NetworkRule] = Field(default_factory=list)
    mach: MachLookupRule = Field(default_factory=MachLookupRule)
    system: SystemRule = Field(default_factory=SystemRule)

    # IOKit open rules (by registry entry class)
    class IOKitOpenRule(BaseModel):
        action: Action = Action.ALLOW
        registry_entry_classes: list[str] = Field(default_factory=list)

        model_config = ConfigDict(extra="forbid")

    iokit: list[IOKitOpenRule] = Field(default_factory=list)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    env: EnvConfig = Field(default_factory=EnvConfig)

    model_config = ConfigDict(extra="forbid")
