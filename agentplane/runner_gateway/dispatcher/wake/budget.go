package wake

import (
	"context"
	"time"
)

// WakeBudgetKey is whose budget a wake attempt spends. ServiceAccounts and Identities use their
// namespace, name, and UID; service callers use their exact SPIFFE ID.
type WakeBudgetKey struct {
	Namespace, Name, UID string
	ServiceSPIFFEID      string
}

// WakeBudget reserves a bounded automatic wake attempt without depending on Dispatcher storage.
// Implementations share reservations across replicas. Explicit resume does not reserve budget.
type WakeBudget interface {
	ReserveWakeAttempt(context.Context, WakeBudgetKey, string, int64) (time.Duration, error)
}
