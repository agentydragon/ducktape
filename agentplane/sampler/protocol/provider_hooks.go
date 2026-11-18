package protocol

import "context"

// ProviderProgress is called only after a semantically meaningful event is valid and durable.
type ProviderProgress func(context.Context) error

// ProviderHooks connects durable provider evidence to runtime progress policy.
type ProviderHooks struct {
	// ResponseHeadRecorded starts the semantic-idle phase after HTTP response evidence is durable.
	ResponseHeadRecorded func()
	// Progress observes a valid, durable semantic event. Keep this separate from the response head.
	Progress ProviderProgress
}
