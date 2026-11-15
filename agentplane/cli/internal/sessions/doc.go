// Package sessions owns Session/Dispatcher and retained-History commands and
// their domain formatting. Command takes invocation services and returns a Cobra
// subtree whose output retains the existing text, JSON, missing/unavailable facts
// and command outcomes. Syntax, flags, per-backend credentials, deadlines and
// observation semantics are preserved; writes are never automatically replayed.
// Signal handling, transport setup and output/error envelopes stay in invocation.
package sessions
