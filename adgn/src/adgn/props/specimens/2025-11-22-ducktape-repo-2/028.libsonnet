local I = import '../../specimens/lib.libsonnet';

// iss-028: docker_client None check should be inside self_check, not at call sites

I.issueOneOccurrence(
  rationale= |||
    The pattern `if self.docker_client is not None: self.self_check(...)` appears
    twice (lines 344-345, 360-361). This conditional is repeated at every call site.

    The check should be internal to self_check() itself, not the caller's
    responsibility. Currently self_check() assumes docker_client is valid (line 342),
    forcing callers to guard it.

    Fix: Move the None check inside self_check():

    def self_check(self, source: str) -> None:
        if self.docker_client is None:
            return  # Skip validation if Docker not available
        run_policy_source(docker_client=self.docker_client, ...)

    Then call sites simplify to: self.self_check(content)

    Benefits:
    - Single responsibility: self_check handles its own preconditions
    - DRY: check not repeated at call sites
    - Cleaner API: callers don't need to know about Docker availability
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [344, 345],  // create_proposal: if self.docker_client is not None
      [360, 361],  // approve_proposal: if self.docker_client is not None
    ],
  },
  gap_note= |||
    This pattern deserves a property like "encapsulate-preconditions" to capture:
    When a method has optional dependencies or preconditions, the method itself
    should handle them (early return, skip, or raise), not force every caller to
    check the precondition before calling.

    Related to but distinct from "no-defensive-programming" - this is about API
    design and encapsulation, not redundant validation.
  |||,
)
