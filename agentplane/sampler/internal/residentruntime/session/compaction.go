package session

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

func (r *Driver) compact(ctx context.Context, owner sessionstore.Owner, live *liveHost, model string) error {
	session, err := r.store.GetSessionForConversation(ctx, owner)
	if err != nil {
		return err
	}
	frontier, err := r.providerFrontier(ctx, owner, session)
	if err != nil {
		return err
	}
	if frontier != nil && frontier.Kind != sessionstore.PendingIntentCompaction {
		return protocol.ErrPrecondition
	}
	if err := r.store.CheckLiveCells(ctx, owner, live != nil && live.code.Err() == nil); err != nil {
		return err
	}
	conversation, err := r.store.Read(ctx, owner)
	if err != nil {
		return err
	}
	if err := conversation.Transcript.CheckSettled(); err != nil {
		return err
	}
	if err := conversation.Context().CheckSettled(); err != nil {
		return err
	}
	metadata, err := r.store.ProviderMetadata(ctx, owner)
	if err != nil {
		return err
	}
	config := r.settings.Models[model]
	adapter, err := adapterFor(config)
	if err != nil {
		return err
	}
	if config.API != conversation.API {
		return protocol.ErrPrecondition
	}
	plan, err := adapter.PrepareCompaction(conversation, session, metadata)
	if err != nil {
		return err
	}
	connection, err := r.settings.Connection(model)
	if err != nil {
		return err
	}
	original := conversation.Context()
	request := sessionstore.ProviderRequestIntent(sessionstore.PendingIntentCompaction, connection, config)
	policy := runtimeProviderAttemptPolicy(r.settings, config, frontier)
	var intentID string
	var wire []byte
	if frontier == nil {
		intentID, wire, err = r.store.BeginCompaction(ctx, owner, request, original, plan.Preparation, policy)
	} else if frontier.RetrySuppressed == sessionstore.ProviderRetrySuppressionHostLoss {
		intentID, wire, err = r.store.BeginCompactionAfterHostLoss(ctx, owner, session.ID, frontier.IntentID, request, original, plan.Preparation, policy)
	} else {
		intentID, wire, err = r.store.BeginCompactionRetry(ctx, owner, frontier.IntentID, original, policy)
	}
	if err != nil {
		return &deferredProviderError{cause: err}
	}
	result, err := r.sampleProvider(ctx, owner, session, intentID, adapter, wire, true)
	if err != nil {
		return err
	}
	replacement, err := plan.Apply(result, intentID)
	if err != nil {
		return r.providerFailure(ctx, owner, session, intentID, err)
	}
	err = r.finishProvider(ctx, owner, intentID, func(writeCtx context.Context) error {
		return r.store.FinishCompaction(writeCtx, owner, intentID, result.Audit, original, replacement, plan.ValidateReplacement)
	})
	if errors.As(err, new(*protocol.KnownFailureError)) {
		return r.providerFailure(ctx, owner, session, intentID, err)
	}
	return err
}
