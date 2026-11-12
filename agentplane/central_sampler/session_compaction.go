package centralsampler

import (
	"context"
	"errors"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

func (r *Runtime) compact(ctx context.Context, owner Owner, model string) error {
	state, err := r.store.ExecutionState(ctx, owner)
	if err != nil {
		return err
	}
	if len(state.Cells) != 0 {
		return &protocol.UnknownEffectError{Reason: "cannot compact over live JavaScript cells"}
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
	session, err := r.store.GetSessionForConversation(ctx, owner)
	if err != nil {
		return err
	}
	environment, err := r.store.environmentContext(conversation, session)
	if err != nil {
		return err
	}
	plan, err := adapter.PrepareCompaction(conversation, session, state, metadata, environmentContextUpdate(nil, environment))
	if err != nil {
		return err
	}
	connection, err := r.settings.Connection(model)
	if err != nil {
		return err
	}
	original := conversation.Context()
	request := providerRequestIntent(PendingIntentCompaction, connection, r.settings.Models[model])
	intentID, wire, err := r.store.BeginCompaction(ctx, owner, request, original, plan.Prepare)
	if err != nil {
		return err
	}
	identity, err := executionIdentity(session)
	if err != nil {
		return err
	}
	client, err := connection.HTTPClient(&http.Client{})
	if err != nil {
		return err
	}
	defer client.CloseIdleConnections()
	result, err := adapter.Sample(ctx, client, connection, wire, &ResponseEvidence{Store: r.store, Owner: owner, IntentID: intentID}, providerAttribution{SessionID: metadata.SessionID, Identity: identity}, true)
	if err != nil {
		return r.rejectKnownCompactionFailure(ctx, owner, intentID, err)
	}
	replacement, err := plan.Apply(result)
	if err != nil {
		return r.rejectKnownCompactionFailure(ctx, owner, intentID, err)
	}
	return r.store.FinishCompaction(ctx, owner, intentID, result.Audit, original, replacement)
}

func (r *Runtime) rejectKnownCompactionFailure(ctx context.Context, owner Owner, intentID string, err error) error {
	var known *protocol.KnownFailureError
	if errors.As(err, &known) {
		if rejectErr := r.store.Reject(ctx, owner, intentID, known); rejectErr != nil {
			return rejectErr
		}
	}
	return err
}
