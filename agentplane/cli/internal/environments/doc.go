// Package environments owns environment discovery and explicit resume commands.
// Command takes invocation services and returns a Cobra subtree that consumes
// exact targets and fresh resume tokens and produces the existing text/ProtoJSON
// and lifecycle errors. Flags, deadlines, target checks and uncertain resume exit
// codes are preserved; resume is submitted once.
// Signal handling, transport setup and envelopes stay in invocation.
package environments
