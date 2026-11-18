# Handing off agent diagnostic RoleBindings from Kyverno to Flux

Flux introduced `agent-diagnostics-metadata` and `agent-diagnostics-logs` alongside Kyverno's
synchronized `agent-readable-*` RoleBindings. Distinct names let both reconcilers run during the
handoff and gave each ownership of its own objects.
[Add Flux readers (#8731)](https://github.com/agentydragon/ducktape/pull/8731).

For an RBAC handoff, verify the replacement RoleBindings against the deployed revision and check
representative allowed and denied requests with each affected identity before removing the old
generator. Check that the replacement grants still hold and the retired bindings are gone after
the cutover. Grants are additive, so a leftover binding can mask a missing permission during
handoff or silently preserve access after a later revocation. Identify leftover bindings by
namespace, name, generator ownership metadata, subjects, and `roleRef` for targeted cleanup.
[Retire the Kyverno generator (#8741)](https://github.com/agentydragon/ducktape/pull/8741).
