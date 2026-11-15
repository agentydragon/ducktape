// Package invocation owns one CLI call's environment, streams, global flags,
// cancellation, deadlines, transport setup and output/error envelopes. New receives
// an environment reader and stdin/stdout/stderr; Execute receives the composed
// command tree and arguments and returns the process status. Families read private
// invocation state through methods and return errors or already-printed outcomes.
// SIGINT/SIGTERM cancel the parent context and retain exit 128+signal; command
// --timeout bounds RPC work, while zero means no deadline (waits read once).
// JSON output and error fields, stdout/stderr separation, bearer presentation,
// retry-disabled connections and existing exit codes remain unchanged. Family
// owners decide safe read retries and reconciliation by ID; no write is resubmitted.
// The command root, Egress and executable wiring stay outside this package.
package invocation
