// Package actions owns Action commands, receipt waiting/recovery and domain
// formatting. Command takes invocation services and returns a Cobra subtree;
// commands consume arguments/files and produce the existing receipts, results,
// text or JSON and typed outcomes. Submission occurs once; an uncertain response
// is reconciled by the same key, under the existing recovery deadline. Read/watch
// retries, terminal exit codes, cancellation, flags and credentials are preserved.
// Signal handling, streams, global deadlines and envelopes stay in invocation.
package actions
