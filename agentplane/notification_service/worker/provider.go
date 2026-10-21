package worker

import (
	"context"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/google/uuid"
)

// Provider reads one kind of subscription source into the inboxes it feeds: the Actions Service, or GitHub.
type Provider interface {
	// Reconcile reads the source of a subscription that is due, and records what is new in the claimed inbox
	// in fenced transactions (a write fails with store.ErrClaimLost once the claim is gone), moving the
	// subscription's next attempt forward. It returns nil when the source was read.
	//
	// A failure the worker should show on the subscription and retry is a *ProviderError, and a source that
	// must not be read again until some time is a *ProviderDeferred. *store.QuotaError is shown as a
	// processing error. Any other error is a bug and stops the worker as an *UnclassifiedProviderError.
	Reconcile(ctx context.Context, claim store.Claim, due store.DueSubscription) error
}

// ProviderError is a failure of a source that is shown on its subscription and retried.
type ProviderError struct {
	Kind store.SourceFailureKind
	// Message is a safe description: never an upstream body, a bearer token or content from a native harness.
	Message string
	// RetryAfter is how long to wait before the source is read again, or zero for DefaultRetry.
	RetryAfter time.Duration
}

// DefaultRetry is the wait after a failure that names none.
const DefaultRetry = 60 * time.Second

func (e *ProviderError) Error() string { return e.Message }

// ProviderDeferred is a source that cannot be read before Until, which is not a failure: the subscription
// shows nothing wrong.
type ProviderDeferred struct{ Until time.Time }

func (e *ProviderDeferred) Error() string {
	return "source deferred until " + e.Until.Format(time.RFC3339)
}

// UnclassifiedProviderError is a Provider failure that is none of the above. The inbox is released first, then
// Run returns it: the process stops rather than hide a bug as a retry.
type UnclassifiedProviderError struct {
	SubscriptionID uuid.UUID
	Err            error
}

func (e *UnclassifiedProviderError) Error() string {
	return fmt.Sprintf("subscription %s: %v", e.SubscriptionID, e.Err)
}

func (e *UnclassifiedProviderError) Unwrap() error { return e.Err }
