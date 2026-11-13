package centralsampler

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

func (r *Runtime) applyInterrupt(ctx context.Context, sessionID string, owner Owner, live *liveHost, interruptIDs []string, pending ...*PendingIntent) (TurnResult, error) {
	var currentPending *PendingIntent
	if len(pending) > 0 {
		currentPending = pending[0]
	} else {
		conversation, err := r.store.Read(ctx, owner)
		if err != nil {
			return "", err
		}
		currentPending = conversation.Pending
	}
	cleanupCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cells, processes := map[string]any{}, map[string]any{}
	var cleanupErr error
	delegatesPending := false
	if live != nil {
		delegatesPending = live.code.HasDelegates()
		live.executor.stopDispatch()
		cells, cleanupErr = live.code.Interrupt(cleanupCtx)
		var processErr error
		processes, processErr = live.executor.InterruptAll(cleanupCtx)
		cleanupErr = errors.Join(cleanupErr, processErr)
		if detachErr := live.code.Detach(cleanupCtx); detachErr != nil {
			cleanupErr = errors.Join(cleanupErr, detachErr)
		}
	}
	state, stateErr := r.store.ExecutionState(cleanupCtx, owner)
	cleanupErr = errors.Join(cleanupErr, stateErr)
	complete := cleanupErr == nil && stateErr == nil && len(state.Cells) == 0
	if delegatesPending {
		complete = false
	}
	if stateErr == nil {
		for _, process := range state.Processes {
			if process.Status != protocol.ProcessExited && process.Status != protocol.ProcessExitedUnread {
				complete = false
			}
		}
	}
	if live != nil {
		if closeErr := live.CloseTransport(); closeErr != nil {
			cleanupErr = errors.Join(cleanupErr, closeErr)
			complete = false
		}
	}
	providerPending := currentPending != nil && (currentPending.Kind == PendingIntentSampling || currentPending.Kind == PendingIntentCompaction)
	result := map[string]any{
		"status": TurnEffectsUnknown, "provider_cancelled": providerPending,
		"provider_completion_unknown": providerPending, "cells": cells, "processes": processes,
		"cleanup_complete": complete, "partial_effects_possible": true,
	}
	if cleanupErr != nil {
		result["cleanup_error"] = cleanupErr.Error()
	}
	status := TurnEffectsUnknown
	if complete {
		status = TurnInterrupted
	}
	result["status"] = string(status)
	if err := r.store.AcknowledgeInterrupt(cleanupCtx, owner, interruptIDs, result); err != nil {
		return "", err
	}
	return status, nil
}
