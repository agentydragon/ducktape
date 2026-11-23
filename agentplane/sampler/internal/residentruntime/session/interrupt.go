package session

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executionstate"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
)

func (r *Driver) applyInterrupt(ctx context.Context, owner sessionstore.Owner, live *liveHost, interruptIDs []string, pending ...*sessionstore.PendingIntent) (sessionstore.TurnResult, error) {
	var currentPending *sessionstore.PendingIntent
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
		if live.executor != nil {
			live.executor.StopDispatch()
		}
		cells, cleanupErr = live.code.Interrupt(cleanupCtx)
		if live.executor != nil {
			var processErr error
			processes, processErr = live.executor.InterruptAll(cleanupCtx)
			cleanupErr = errors.Join(cleanupErr, processErr)
		}
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
			if process.Status != executionstate.ProcessExited && process.Status != executionstate.ProcessExitedUnread {
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
	providerPending := currentPending != nil && (currentPending.Kind == sessionstore.PendingIntentSampling || currentPending.Kind == sessionstore.PendingIntentCompaction)
	result := map[string]any{
		"status": sessionstore.TurnEffectsUnknown, "provider_cancelled": providerPending,
		"provider_completion_unknown": providerPending, "cells": cells, "processes": processes,
		"cleanup_complete": complete, "partial_effects_possible": true,
	}
	if cleanupErr != nil {
		result["cleanup_error"] = cleanupErr.Error()
	}
	status := sessionstore.TurnEffectsUnknown
	if complete {
		status = sessionstore.TurnInterrupted
	}
	result["status"] = string(status)
	if err := r.store.AcknowledgeInterrupt(cleanupCtx, owner, interruptIDs, result); err != nil {
		return "", err
	}
	return status, nil
}
