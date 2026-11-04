# Ownership subsets with full spec context

1. Add a normal-run ownership allowlist after complete selector resolution and
   analysis-contract collection. Use typed chunk/module identities; reject
   unknown or duplicate entries. Keep the usual assignment gate and emission.
   Initially require `inline_in_entry`, the policy used by the downstream
   canonical specs; reject other residual modes and full graph export explicitly.
2. Build the native CLI, publish the reviewed engine change, and collect matching
   analyses for the downstream canonical inputs. No tests are authorized in this
   round; use builds, normal production-spec runs, output review and hosted CI.
3. Start with no retained modules, then restore large coherent groups with the
   same full selector and assertion context. Retain useful passing output and
   isolate failing combinations. Check normal gate/translation verdicts and
   emitted architecture; partial acceptance does not establish full acceptance.

Selected module names and explicit entry renames stay intact. Omitted module
members retain source names in entry: separate modules can legitimately reuse
the same recovered name, so promoting every name into entry would collide.
The authored spec and its names remain unchanged. Record the selected module
paths in each chunk's reports. Do not change analysis projection semantics or
bypass compiled-engine/source/projection checks to reuse an old artifact.
