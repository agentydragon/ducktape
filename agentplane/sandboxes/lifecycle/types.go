// Package lifecycle owns policy and guarded desired-state changes for existing Sandboxes.
package lifecycle

import (
	"context"
	"time"
)

// ServiceAccount identifies the effective caller by its live Kubernetes identity.
// The UID distinguishes a replacement account with the same namespace and name.
type ServiceAccount struct {
	Namespace string
	Name      string
	UID       string
}

// WakeBudget reserves a bounded wake attempt without depending on Dispatcher storage.
// Implementations share reservations across replicas and explicit and automatic wake paths.
type WakeBudget interface {
	ReserveWakeAttempt(context.Context, ServiceAccount, string, int64) (time.Duration, error)
}
