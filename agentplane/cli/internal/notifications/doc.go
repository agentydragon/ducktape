// Package notifications owns the Notification command family and its domain
// formatting. Command takes invocation services and returns a Cobra subtree;
// commands read flags/environment/files and write existing text or ProtoJSON to
// the supplied streams. Syntax, credentials, cursor/ack semantics, idempotency,
// deadlines and error classification are preserved; no mutation is retried.
// Signal handling, shared flags, connection setup and envelopes stay in invocation.
package notifications
