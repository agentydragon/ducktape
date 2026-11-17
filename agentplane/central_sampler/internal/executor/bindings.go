package executor

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/executor/transport"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

// storeBindings binds transport journals and session transitions to one fenced owner.
// Durable process retirement remains in Store's existing session transactions.
type storeBindings struct {
	store             *sessionstore.Store
	owner             sessionstore.Owner
	workspacePrepared *atomic.Bool
}

func nestedOutcome(outcome transport.Outcome) sessionstore.NestedOutcome {
	return sessionstore.NestedOutcome(outcome)
}

func nestedOutcomePointer(outcome *transport.Outcome) *sessionstore.NestedOutcome {
	if outcome == nil {
		return nil
	}
	value := nestedOutcome(*outcome)
	return &value
}

func (b *storeBindings) BeginExecutorIntent(ctx context.Context, request map[string]any) (string, error) {
	return b.store.RecordNested(ctx, b.owner, sessionstore.IntentExecutor, request)
}

func (b *storeBindings) FinishExecutorIntent(ctx context.Context, intentID string, evidence any) error {
	return b.store.FinishNested(ctx, b.owner, intentID, evidence)
}

func (b *storeBindings) RecordExecutorNativeFrame(ctx context.Context, frame transport.NativeFrame) error {
	payload := map[string]any{
		"request_id": frame.OperationID, "execution_session_id": frame.ExecutionSession,
		"payload": frame.Payload,
	}
	if frame.OmitPayload {
		payload["payload_digest_sha256"] = frame.PayloadDigestSHA256
		payload["payload_bytes"] = frame.PayloadBytes
		delete(payload, "payload")
	}
	phase, err := nativeFramePhase(frame.Direction)
	if err != nil {
		return err
	}
	return b.store.ProtocolEvent(ctx, b.owner, frame.OperationID, eventpb.ExecutionProgress_KIND_EXECUTOR, phase, payload)
}

func nativeFramePhase(direction string) (eventpb.ExecutionProgress_Phase, error) {
	switch direction {
	case "request":
		return eventpb.ExecutionProgress_PHASE_REQUEST, nil
	case "response":
		return eventpb.ExecutionProgress_PHASE_RESPONSE, nil
	case "notification":
		return eventpb.ExecutionProgress_PHASE_NOTIFICATION, nil
	default:
		return eventpb.ExecutionProgress_PHASE_UNSPECIFIED, fmt.Errorf("executor native frame has unknown direction=%q", direction)
	}
}

func (b *storeBindings) CurrentSession(ctx context.Context) (string, bool, error) {
	conversation, err := b.store.Read(ctx, b.owner)
	if err != nil {
		return "", false, err
	}
	if conversation.ExecutorSession == nil {
		return "", false, nil
	}
	return *conversation.ExecutorSession, true, nil
}

func (b *storeBindings) BindSession(ctx context.Context, sessionID string, shell *string) error {
	return b.store.SetExecutorSession(ctx, b.owner, sessionID, shell)
}

func (b *storeBindings) BindInitialSession(ctx context.Context, sandboxUID, incarnationUID, sessionID string, shell *string) error {
	return b.store.BindInitialExecutorSession(ctx, b.owner, sandboxUID, incarnationUID, sessionID, shell)
}

func (b *storeBindings) RetireLostSession(ctx context.Context, sessionID, notDispatchedProcessID string) error {
	return b.store.RetireLostExecutorSession(ctx, b.owner, sessionID, notDispatchedProcessID)
}

func (b *storeBindings) RebindIncarnation(ctx context.Context, sandboxUID, previousUID, replacementUID, notDispatchedProcessID string) error {
	if err := b.store.RebindExecutorIncarnation(ctx, b.owner, sandboxUID, previousUID, replacementUID, notDispatchedProcessID); err != nil {
		return err
	}
	b.workspacePrepared.Store(false)
	return nil
}

func (b *storeBindings) RebindIncarnationWithSession(ctx context.Context, sandboxUID, previousUID, replacementUID, sessionID string, shell *string) error {
	if err := b.store.RebindExecutorIncarnationWithSession(ctx, b.owner, sandboxUID, previousUID, replacementUID, sessionID, shell); err != nil {
		return err
	}
	b.workspacePrepared.Store(false)
	return nil
}

// processChangeBroadcaster coalesces stream and process wakeups. Stored cursors,
// rather than notification count, determine which output has been consumed.
type processChangeBroadcaster struct {
	mu      sync.Mutex
	changed chan struct{}
}

func (b *processChangeBroadcaster) Changes() <-chan struct{} {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.changed == nil {
		b.changed = make(chan struct{})
	}
	return b.changed
}

func (b *processChangeBroadcaster) SignalProcessChange() {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.changed != nil {
		close(b.changed)
	}
	b.changed = make(chan struct{})
}
