package protocol

// ProviderHooks connects durable provider evidence to runtime progress policy.
type ProviderHooks struct {
	// ResponseHeadRecorded starts the semantic-idle phase after HTTP response evidence is durable.
	ResponseHeadRecorded func()
	// Progress observes a valid, durable semantic event. Keep this separate from the response head.
	Progress func() error
}
