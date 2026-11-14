package residentruntime

import (
	"context"
	"errors"
	"maps"
	"net/http"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/providerio"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

var (
	errProviderRecovery      = errors.New("provider recovery frontier changed")
	errProviderCommandFailed = errors.New("provider command failed")
)

// A deferred provider step retains its durable state for a successor. It must not pass through
// generic effect-failure settlement, which would block the Session or forgive unrelated work.
type deferredProviderError struct{ cause error }

func (e *deferredProviderError) Error() string { return e.cause.Error() }
func (e *deferredProviderError) Unwrap() error { return e.cause }

func runtimeProviderAttemptPolicy(settings samplersettings.Settings) sessionstore.ProviderAttemptPolicy {
	return sessionstore.ProviderAttemptPolicy{Budget: 3, Lease: settings.ProviderAttemptLease()}
}

func providerBackoff(number int, hint time.Duration) time.Duration {
	delay := time.Second
	for i := 1; i < number && delay < sessionstore.MaxProviderBackoff; i++ {
		delay *= 2
	}
	return min(max(delay, hint), sessionstore.MaxProviderBackoff)
}

func providerFailureReason(reason string) string {
	reason = strings.ToValidUTF8(reason[:min(len(reason), 4096)], "")
	if strings.TrimSpace(reason) == "" {
		return "provider request failed"
	}
	return reason
}

func (r *Runtime) providerProgress(owner sessionstore.Owner, intentID string) protocol.ProviderProgress {
	return func(ctx context.Context) error {
		return r.store.RenewProviderAttempt(ctx, owner, intentID, r.settings.ProviderProgressLease())
	}
}

func (r *Runtime) providerFailure(ctx context.Context, owner sessionstore.Owner, session *sessionstore.Session, intentID string, cause error) error {
	var persistence *protocol.ProviderEvidencePersistenceError
	var progress *protocol.ProviderProgressError
	var lost *sessionstore.LostOwnershipError
	if ctx.Err() != nil || errors.As(cause, &persistence) || errors.As(cause, &progress) || errors.As(cause, &lost) || r.isDraining() {
		return &deferredProviderError{cause: cause}
	}
	attempt, err := r.store.CurrentProviderAttempt(ctx, owner, session.ID)
	if err != nil {
		return &deferredProviderError{cause: err}
	}
	if attempt == nil || attempt.IntentID != intentID {
		return &deferredProviderError{cause: cause}
	}
	failure := sessionstore.ProviderAttemptFailure{Class: sessionstore.ProviderAttemptPermanent, Reason: providerFailureReason(cause.Error())}
	var transient *protocol.ProviderTransientError
	var phase *protocol.ProviderPhaseTimeoutError
	if errors.As(cause, &transient) {
		failure.Class, failure.RetryAfter = sessionstore.ProviderAttemptTransient, providerBackoff(attempt.Number, transient.RetryAfter)
	} else if errors.As(cause, &phase) {
		failure.Class, failure.RetryAfter = sessionstore.ProviderAttemptTransient, providerBackoff(attempt.Number, 0)
	}
	if err := r.store.AbandonProviderAttempt(ctx, owner, intentID, failure); err != nil {
		return &deferredProviderError{cause: err}
	}
	return errProviderRecovery
}

func (r *Runtime) failProviderFrontier(ctx context.Context, owner sessionstore.Owner, session *sessionstore.Session, frontier *sessionstore.ProviderAttempt, reason string) error {
	if reason == "" {
		reason = "provider attempt budget exhausted"
	}
	reason = providerFailureReason(reason)
	if err := r.store.FailProviderCommand(ctx, owner, session.ID, frontier.CommandID, frontier.IntentID, reason); err != nil {
		return &deferredProviderError{cause: err}
	}
	return errProviderCommandFailed
}

// This runs only between joined provider calls, never alongside an upstream request.
func (r *Runtime) providerFrontier(ctx context.Context, owner sessionstore.Owner, session *sessionstore.Session) (*sessionstore.ProviderAttempt, error) {
	current, err := r.store.CurrentProviderAttempt(ctx, owner, session.ID)
	if err != nil {
		return nil, &deferredProviderError{cause: err}
	}
	if current != nil {
		if err := r.store.AbandonProviderAttempt(ctx, owner, current.IntentID, sessionstore.ProviderAttemptFailure{
			Class: sessionstore.ProviderAttemptTransient, Reason: "provider attempt ended without a committed result",
			RetryAfter: providerBackoff(current.Number, 0),
		}); err != nil {
			return nil, &deferredProviderError{cause: err}
		}
	}
	frontier, err := r.store.ProviderRecoveryFrontier(ctx, owner, session.ID)
	if err != nil {
		return nil, &deferredProviderError{cause: err}
	}
	if frontier == nil {
		return nil, nil
	}
	if frontier.FailureClass == sessionstore.ProviderAttemptInterrupted || frontier.RetrySuppressed == sessionstore.ProviderRetrySuppressionInterrupted {
		return nil, &deferredProviderError{cause: errors.New("interrupted provider command awaits interrupt settlement")}
	}
	if frontier.FailureClass == sessionstore.ProviderAttemptDraining {
		return nil, &deferredProviderError{cause: errors.New("draining provider command awaits a successor")}
	}
	if frontier.Number >= frontier.Budget || frontier.FailureClass == sessionstore.ProviderAttemptPermanent {
		return nil, r.failProviderFrontier(ctx, owner, session, frontier, frontier.FailureReason)
	}
	if frontier.RetrySuppressed != sessionstore.ProviderRetrySuppressionHostLoss && !frontier.Retryable {
		return nil, r.failProviderFrontier(ctx, owner, session, frontier, frontier.FailureReason)
	}
	if frontier.Retryable && !frontier.Ready {
		return nil, errProviderRecovery
	}
	config, ok := r.settings.Models[session.Model]
	connection, err := r.settings.Connection(session.Model)
	if !ok || err != nil || frontier.ProviderRequest == nil {
		return nil, r.failProviderFrontier(ctx, owner, session, frontier, "provider route is no longer configured")
	}
	expected := sessionstore.ProviderRequestIntent(frontier.Kind, connection, config).ProviderRequest
	stored := frontier.ProviderRequest
	if expected.API != stored.API || expected.Connection != stored.Connection || expected.ProviderModel != stored.ProviderModel ||
		expected.Method != stored.Method || expected.URL != stored.URL || !maps.Equal(expected.Headers, stored.Headers) {
		return nil, r.failProviderFrontier(ctx, owner, session, frontier, "provider route changed during recovery")
	}
	if !sessionstore.ProviderRequestProjectionMatches(config, stored) {
		return nil, r.failProviderFrontier(ctx, owner, session, frontier, "provider parser profile changed during recovery")
	}
	if frontier.Kind == sessionstore.PendingIntentCompaction && ((expected.CompactionControl == nil) != (stored.CompactionControl == nil) ||
		expected.CompactionControl != nil && *expected.CompactionControl != *stored.CompactionControl) {
		return nil, r.failProviderFrontier(ctx, owner, session, frontier, "compaction suffix or effort policy changed during recovery")
	}
	return frontier, nil
}

func (r *Runtime) finishProvider(ctx context.Context, owner sessionstore.Owner, intentID string, persist func(context.Context) error) error {
	err := persist(ctx)
	if err == nil {
		return nil
	}
	// Re-read the exact intent even if the caller's context expired while COMMIT was in flight.
	readCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
	defer cancel()
	state, readErr := r.store.ReadProviderFinish(readCtx, owner, intentID)
	if readErr != nil {
		return &deferredProviderError{cause: errors.Join(err, readErr)}
	}
	if state.Completed {
		return nil
	}
	if !state.Open {
		return &deferredProviderError{cause: err}
	}
	// The complete response is still in memory. Retry persistence, never the provider request.
	if retryErr := persist(readCtx); retryErr == nil {
		return nil
	} else {
		err = errors.Join(err, retryErr)
	}
	state, readErr = r.store.ReadProviderFinish(readCtx, owner, intentID)
	if readErr == nil && state.Completed {
		return nil
	}
	return &deferredProviderError{cause: errors.Join(err, readErr)}
}

// The execution permit is released by the caller; ownership and the live host remain resident.
func (r *Runtime) waitProviderRecovery(ctx context.Context, owner sessionstore.Owner, sessionID string, task *residentSession, live *liveHost) error {
	for {
		if r.isDraining() {
			return &deferredProviderError{cause: errors.New("provider recovery paused for drain")}
		}
		interrupts, err := r.store.PendingInterrupts(ctx, owner)
		if err != nil {
			return &deferredProviderError{cause: err}
		}
		if len(interrupts) > 0 {
			return nil
		}
		frontier, err := r.store.ProviderRecoveryFrontier(ctx, owner, sessionID)
		if err != nil {
			return &deferredProviderError{cause: err}
		}
		if frontier == nil || !frontier.Retryable || frontier.Ready {
			return nil
		}
		timer := time.NewTimer(frontier.RetryIn)
		select {
		case <-ctx.Done():
			timer.Stop()
			return &deferredProviderError{cause: ctx.Err()}
		case <-r.draining:
			timer.Stop()
			return &deferredProviderError{cause: errors.New("provider recovery paused for drain")}
		case <-liveDone(live):
			timer.Stop()
			return nil
		case <-task.wake:
			timer.Stop()
		case <-timer.C:
		}
	}
}

func (r *Runtime) sampleProvider(ctx context.Context, owner sessionstore.Owner, session *sessionstore.Session, intentID string, adapter protocolAdapter,
	client *http.Client, wire []byte, compact bool,
) (*sampleResult, error) {
	connection, err := r.settings.Connection(session.Model)
	if err != nil {
		return nil, r.providerFailure(ctx, owner, session, intentID, err)
	}
	identity, err := executionIdentity(session)
	if err != nil {
		return nil, r.providerFailure(ctx, owner, session, intentID, err)
	}
	metadata, err := r.store.ProviderMetadata(ctx, owner)
	if err != nil {
		return nil, &deferredProviderError{cause: err}
	}
	client, err = connection.HTTPClient(client)
	if err != nil {
		return nil, r.providerFailure(ctx, owner, session, intentID, err)
	}
	client = providerio.WithProviderResponseHeaderTimeout(client, r.settings.ProviderResponseHeaderTimeout())
	if compact || connection.TLS != nil {
		defer client.CloseIdleConnections()
	}
	providerCtx, hooks, stop := providerio.WithProviderIdleTimeout(ctx, r.settings.ProviderIdleTimeout(), r.providerProgress(owner, intentID))
	defer stop()
	result, err := adapter.Sample(providerCtx, client, connection, wire, &sessionstore.ResponseEvidence{Store: r.store, Owner: owner, IntentID: intentID},
		providerAttribution{SessionID: metadata.SessionID, Identity: identity}, compact, hooks)
	if err != nil {
		return nil, r.providerFailure(ctx, owner, session, intentID, err)
	}
	return result, nil
}
