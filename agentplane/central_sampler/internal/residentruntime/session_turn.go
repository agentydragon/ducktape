package residentruntime

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

func (r *Runtime) controlledAdvance(ctx context.Context, sessionID string, owner sessionstore.Owner, live *liveHost, client *http.Client, session *sessionstore.Session) (sessionstore.TurnResult, error) {
	interrupts, err := r.store.PendingInterrupts(ctx, owner)
	if err != nil {
		return "", err
	}
	if len(interrupts) > 0 {
		return r.applyInterrupt(ctx, sessionID, owner, live, interrupts)
	}
	advanceCtx, cancel := context.WithCancel(ctx)
	type advanceResult struct {
		result sessionstore.TurnResult
		err    error
	}
	completed := make(chan advanceResult, 1)
	go func() {
		result, err := r.advance(advanceCtx, owner, live, client, session)
		completed <- advanceResult{result: result, err: err}
	}()
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
	joined := false
	defer func() {
		cancel()
		if !joined {
			<-completed
		}
	}()
	for {
		select {
		case result := <-completed:
			joined = true
			return result.result, result.err
		case <-ctx.Done():
			cancel()
			<-completed
			joined = true
			return "", ctx.Err()
		case <-liveDone(live):
			cancel()
			<-completed
			joined = true
			if err := live.code.Err(); err != nil {
				return "", err
			}
			return "", errors.New("code-mode session detached during advance")
		case <-ticker.C:
			interrupts, err := r.store.PendingInterrupts(ctx, owner)
			if err != nil {
				return "", err
			}
			if len(interrupts) == 0 && live != nil && live.code.HasForeground() {
				steering, err := r.store.HasSteeringInput(ctx, owner, sessionID)
				if err != nil {
					return "", err
				}
				if steering {
					if _, err := live.code.YieldForeground(ctx); err != nil {
						return "", err
					}
				}
			}
			if len(interrupts) > 0 {
				conversation, readErr := r.store.Read(ctx, owner)
				if readErr != nil {
					return "", readErr
				}
				cancel()
				<-completed
				joined = true
				return r.applyInterrupt(ctx, sessionID, owner, live, interrupts, conversation.Pending)
			}
		}
	}
}

func (r *Runtime) advance(ctx context.Context, owner sessionstore.Owner, live *liveHost, client *http.Client, session *sessionstore.Session) (sessionstore.TurnResult, error) {
	conversation, err := r.store.Read(ctx, owner)
	if err != nil {
		return "", err
	}
	if conversation.Stopped && conversation.Pending == nil {
		return sessionstore.TurnInterrupted, nil
	}
	frontier, err := r.providerFrontier(ctx, owner, session)
	if err != nil {
		return "", err
	}
	if frontier != nil && frontier.Kind == sessionstore.PendingIntentCompaction {
		if err := r.compact(ctx, owner, session.Model); err != nil {
			return "", err
		}
		return sessionstore.TurnCompact, nil
	}
	config, configured := r.settings.Models[session.Model]
	if !configured || conversation.API != config.API {
		return "", protocol.ErrPrecondition
	}

	if conversation.Pending != nil {
		return "", &protocol.UnknownEffectError{Reason: "unresolved provider or executor intent blocks continuation; automatic replay is unsafe"}
	}
	if conversation.Stopped {
		return sessionstore.TurnInterrupted, nil
	}
	if err := r.store.CheckLiveCells(ctx, owner, live != nil); err != nil {
		return "", err
	}
	state, err := r.store.ExecutionState(ctx, owner)
	if err != nil {
		return "", err
	}
	if live != nil {
		if err := live.code.Err(); err != nil {
			return "", err
		}
	}
	calls, err := conversation.Transcript.PendingCalls()
	if err != nil {
		return "", err
	}
	if len(calls) > 0 {
		call := &calls[0]
		if !call.IsModelTool(r.settings.Models[session.Model].ExecEncoding) {
			if !call.HasValidShape() {
				return "", &protocol.KnownFailureError{Evidence: map[string]any{"reason": "invalid model tool call shape", "call": call}}
			}
			if call.IsConfiguredToolName(r.settings.Models[session.Model].ExecEncoding) {
				return "", &protocol.KnownFailureError{Evidence: map[string]any{"reason": "unsupported model tool encoding", "call": call}}
			}
			content := []protocol.ContentPart{{Type: "input_text", Text: unsupportedToolCallMessage(*call)}}
			notDispatched := false
			intentID, _, err := r.store.Begin(ctx, owner, sessionstore.IntentPayload{Kind: sessionstore.PendingIntentCell, Call: call, Dispatched: &notDispatched}, nil, nil)
			if err != nil {
				return "", err
			}
			result := map[string]any{
				"call_id": call.CallID, "output": content,
				"success": false, "dispatched": false,
			}
			if err := r.store.FinishTool(ctx, owner, intentID, result, *call, content); err != nil {
				return "", err
			}
			return sessionstore.TurnCell, nil
		}
		if live == nil {
			return "", &protocol.KnownFailureError{Evidence: map[string]any{"reason": "pending code cell has no live host", "call": call}}
		}
		intentID, _, err := r.store.Begin(ctx, owner, sessionstore.IntentPayload{Kind: sessionstore.PendingIntentCell, Call: call}, nil, nil)
		if err != nil {
			return "", err
		}
		content, err := live.code.Execute(ctx, *call)
		if err != nil {
			var hostError *codemode.ResponseError
			if errors.As(err, &hostError) && hostError.Phase == codemode.OperationResponsePhase {
				content = []protocol.ContentPart{{Type: "input_text", Text: hostError.Message}}
				result := map[string]any{
					"call_id": call.CallID, "output": content, "success": false,
					"code_mode_host_response": hostError.Evidence(),
				}
				if err := r.store.FinishTool(ctx, owner, intentID, result, *call, content); err != nil {
					return "", err
				}
				return sessionstore.TurnCell, nil
			}
			var known *protocol.KnownFailureError
			if errors.As(err, &known) {
				if rejectErr := r.store.Reject(ctx, owner, intentID, known); rejectErr != nil {
					return "", rejectErr
				}
			}
			return "", err
		}
		result := map[string]any{"call_id": call.CallID, "output": content}
		if err := r.store.FinishTool(ctx, owner, intentID, result, *call, content); err != nil {
			return "", err
		}
		return sessionstore.TurnCell, nil
	}
	if frontier == nil {
		// TODO: Codex rust-v0.162.1 keeps notify() outputs and user input in one arrival-ordered pending
		// queue; this records a step's notify() outputs before its steering input (SPEC.md deviation).
		drained, err := r.store.DrainNotifications(ctx, owner)
		if err != nil {
			return "", err
		}
		if drained {
			return sessionstore.TurnNotification, nil
		}
		// TODO: Codex's InstantInterrupt also cuts an in-flight provider stream for new input. Steering
		// input waits for the prepared provider step (including retries) to settle; cutting that
		// step to prepare a new steering context remains a separate scheduling change.
		steered, err := r.store.DrainSteeringInput(ctx, owner, session.ID)
		if err != nil {
			return "", err
		}
		if steered {
			return sessionstore.TurnSteer, nil
		}
		if conversation.Transcript.TurnComplete() {
			return sessionstore.TurnDone, nil
		}
	}
	adapter, err := adapterFor(config)
	if err != nil {
		return "", err
	}
	if conversation.API != config.API {
		return "", protocol.ErrPrecondition
	}
	threshold := adapter.AutoCompactTokens(r.settings.AutoCompactTokens)

	if frontier == nil && threshold != nil && conversation.ContextTokens != nil && *conversation.ContextTokens >= *threshold && len(state.Cells) == 0 {
		if err := r.compact(ctx, owner, session.Model); err != nil {
			return "", err
		}
		return sessionstore.TurnCompact, nil
	}
	if frontier == nil || frontier.RetrySuppressed == sessionstore.ProviderRetrySuppressionHostLoss {
		conversation, err = r.store.PrepareSamplingContext(ctx, owner, state)
		if err != nil {
			return "", &deferredProviderError{cause: err}
		}
	}

	metadata, err := r.store.ProviderMetadata(ctx, owner)
	if err != nil {
		return "", err
	}
	connection, err := r.settings.Connection(session.Model)
	if err != nil {
		return "", err
	}
	preparation := adapter.Prepare(session, metadata)
	request := sessionstore.ProviderRequestIntent(sessionstore.PendingIntentSampling, connection, config)
	policy := runtimeProviderAttemptPolicy(r.settings)
	var intentID string
	var wire []byte
	if frontier == nil {
		intentID, wire, err = r.store.Begin(ctx, owner, request, preparation, &policy)
	} else if frontier.RetrySuppressed == sessionstore.ProviderRetrySuppressionHostLoss {
		intentID, wire, err = r.store.BeginSamplingAfterHostLoss(ctx, owner, session.ID, frontier.IntentID, request, preparation, policy)
	} else {
		intentID, wire, err = r.store.BeginSamplingRetry(ctx, owner, frontier.IntentID, policy)
	}
	if err != nil {
		return "", &deferredProviderError{cause: err}
	}
	response, err := r.sampleProvider(ctx, owner, session, intentID, adapter, client, wire, false)
	if err != nil {
		return "", err
	}
	candidate := conversation.Transcript.Clone()
	err = candidate.AppendRecords(response.Records)
	if err == nil {
		var pending []protocol.ToolInvocation
		pending, err = candidate.PendingCalls()
		for _, call := range pending {
			if !call.HasValidShape() || (call.IsConfiguredToolName(config.ExecEncoding) && !call.IsModelTool(config.ExecEncoding)) {
				err = &protocol.KnownFailureError{Evidence: map[string]any{"reason": "invalid model tool encoding", "call": call}}
				break
			}
		}
	}
	if err != nil {
		failure := &protocol.KnownFailureError{Evidence: map[string]any{"reason": "invalid native conversation continuation", "response": response.Audit, "error": err.Error()}}
		return "", r.providerFailure(ctx, owner, session, intentID, failure)
	}
	if err := r.finishProvider(ctx, owner, intentID, func(writeCtx context.Context) error {
		return r.store.Finish(writeCtx, owner, intentID, response.Audit, response.Records, response.ContextTokens)
	}); err != nil {
		return "", err
	}
	return sessionstore.TurnSample, nil
}
