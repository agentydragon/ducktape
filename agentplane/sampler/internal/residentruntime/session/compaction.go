package session

import (
	"context"
	"errors"
	"net/http"

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
	adapter, err := adapterFor(r.settings.Models[model])
	if err != nil {
		return err
	}
	if adapter.API() != conversation.API {
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
	request := sessionstore.ProviderRequestIntent(sessionstore.PendingIntentCompaction, connection, r.settings.Models[model])
	policy := runtimeProviderAttemptPolicy(r.settings, r.settings.Models[model], frontier)
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
	result, err := r.sampleProvider(ctx, owner, session, intentID, adapter, &http.Client{}, wire, true)
	if err != nil {
		return err
	}
	replacement, err := plan.Apply(result)
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
