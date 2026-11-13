package sources

import (
	"context"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/google/uuid"
)

// Provider processes one due subscription and commits any resulting entries through fenced store operations.
type Provider interface {
	Process(ctx context.Context, claim store.Claim, due store.DueSubscription) error
}

// ProviderError is a source failure the coordinator records on the subscription and retries.
type ProviderError struct {
	Kind store.SourceFailureKind
	// Message is a safe description: never an upstream body, a bearer token or content from a native harness.
	Message    string
	RetryAfter time.Duration
}

func (e *ProviderError) Error() string { return e.Message }

// ProviderDeferred postpones a read without recording a failure.
type ProviderDeferred struct{ Until time.Time }

func (e *ProviderDeferred) Error() string {
	return "source deferred until " + e.Until.Format(time.RFC3339)
}

// DefaultRetry is the wait after a source failure that names no retry interval.
const DefaultRetry = 60 * time.Second

// UnclassifiedProviderError is a provider failure outside its contract. The claim is released before this is
// returned so the process can stop without holding source work until the lease expires.
type UnclassifiedProviderError struct {
	SubscriptionID uuid.UUID
	Err            error
}

func (e *UnclassifiedProviderError) Error() string {
	return fmt.Sprintf("subscription %s: %v", e.SubscriptionID, e.Err)
}

func (e *UnclassifiedProviderError) Unwrap() error { return e.Err }
