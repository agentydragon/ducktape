# Hosted CI authentication header in RC announcements

Hosted invocation `936c4818-b0df-5978-bddc-eee38b667f22` (PR #14710 rerun)
printed an authentication header while Bazel announced its rc options. This
report omits the value.

The value-suppressed source check matched the header only in the runner-created
`/home/buildbuddy/workspace/buildbuddy.bazelrc`. The same invocation log showed
that file, the checked-out `.bazelrc`, and `devinfra/bazel/rbe.bazelrc` as rc
sources. The CI workflow invokes `bazel_ci.py` with `--config=ci`; the workspace
configuration had `common:ci --announce_rc`, which caused Bazel to print options
from the generated runner rc. The generated authentication source is outside
this repository; the repository now explicitly sets `common:ci --noannounce_rc`.

The raw Build Event Protocol stream for that invocation contained both
`OptionsParsed` and structured `CommandLine` events (159 events total). An
in-memory scan found other `remote_header` options but no
`x-buildbuddy-api-key` option or matching value. This is evidence about that
captured event stream only; it does not establish that every BuildBuddy metadata
surface redacts credentials.

Validation used a synthetic header value in an extra rc file, never the
credential. From this clean checkout, `bazelisk --nosystem_rc --nohome_rc
--bazelrc=<synthetic-rc> info --config=ci` exited successfully and did not
announce the canary under the explicit `common:ci --noannounce_rc` policy. The
same command with explicit `--announce_rc` did announce it, confirming the
canary check detects enabled announcements. The probe removed
`BUILDBUDDY_API_KEY` from its environment and created no BuildBuddy invocation.
The diff leaves CI config selection and the RBE/auth configuration unchanged.

The historical BuildBuddy log still contains the previously announced value.
This change does not delete stored artifacts or rotate credentials; those
require the existing credential owner's rotation and retention procedures.

References: [Bazel CI workflow](../../../../.github/workflows/bazel-ci.yml),
[Bazel CI driver](../../../../devinfra/ci/bazel_ci.py),
[workspace Bazel configuration](../../../../.bazelrc),
[BuildBuddy remote runner configuration](../../docs/remote.md), and
[Bazel Build Event Protocol glossary](https://bazel.build/versions/9.2.0/remote/bep-glossary).
