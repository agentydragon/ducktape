from mcp_infra._markers import REQUIRES_SANDBOX_EXEC
from mcp_infra.seatbelt.compile import compile_sbpl
from mcp_infra.seatbelt.model import (
    DefaultBehavior,
    FileOp,
    FileRule,
    MachLookupRule,
    NetworkOp,
    NetworkRule,
    ProcessRule,
    SBPLPolicy,
    Subpath,
    SystemRule,
    TraceConfig,
)

pytestmark = [*REQUIRES_SANDBOX_EXEC]


def test_compile_allow_all_effectively_no_sandbox():
    policy = SBPLPolicy(
        default_behavior=DefaultBehavior.ALLOW,
        process=ProcessRule(allow_process_star=True, allow_signal_self=True),
        files=[
            FileRule(op=FileOp.FILE_READ_STAR, filters=[Subpath(subpath="/")]),
            FileRule(op=FileOp.FILE_WRITE_STAR, filters=[Subpath(subpath="/")]),
            FileRule(op=FileOp.FILE_MAP_EXECUTABLE, filters=[]),
        ],
        network=[
            NetworkRule(op=NetworkOp.NETWORK_INBOUND, local_only=False),
            NetworkRule(op=NetworkOp.NETWORK_OUTBOUND, local_only=False),
            NetworkRule(op=NetworkOp.NETWORK_BIND, local_only=False),
        ],
        mach=MachLookupRule(global_names=[]),
        system=SystemRule(system_socket=True, sysctl_read=True),
        trace=TraceConfig(enabled=False),
    )

    sb = compile_sbpl(policy)

    # Core header & defaults
    assert "(version 1)" in sb
    assert "(allow default)" in sb

    # Process primitives
    assert "(allow process*)" in sb
    assert "(allow signal (target self))" in sb

    # FS broad rules
    assert '(allow file-read* (subpath "/"))' in sb
    assert '(allow file-write* (subpath "/"))' in sb
    assert "(allow file-map-executable)" in sb

    # Network wide open
    assert "(allow network-inbound)" in sb
    assert "(allow network-outbound)" in sb
    assert "(allow network-bind)" in sb

    # System toggles
    assert "(allow system-socket)" in sb
    assert "(allow sysctl-read)" in sb

    # Sanity: should not contain any deny lines in this configuration
    assert "(deny" not in sb
