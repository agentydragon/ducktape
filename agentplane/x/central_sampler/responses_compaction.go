package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
)

func (r *Runtime) compactResponses(ctx context.Context, owner Owner, model string, conversation *Conversation, metadata SessionAttribution, state *ExecutionState) error {
	original := conversation.Context()
	body, err := BuildResponsesCompactionRequest(ResponsesRequestParams{
		Model: r.settings.Models[model].ProviderModel, Attribution: metadata,
		Items: original.(*ResponsesContext).Items, Instructions: conversation.Instructions, ExecutionState: state,
	}, r.settings.Models[model])
	if err != nil {
		return err
	}
	connection, err := r.settings.Connection(model)
	if err != nil {
		return err
	}
	credential, err := connection.Auth.ReadCredential()
	if err != nil {
		return err
	}
	intentID, err := r.store.BeginCompaction(ctx, owner, providerRequestAudit("compaction", connection, r.settings.Models[model], body, mustJSON(body)), original)
	if err != nil {
		return err
	}
	client := &http.Client{}
	headers, err := r.store.ProviderHeaders(ctx, owner)
	if err != nil {
		return err
	}
	response, err := Sample(ctx, client, connection.BaseURL, credential, body, r.settings.Models[model].Responses,
		&ResponseEvidence{Store: r.store, Owner: owner, IntentID: intentID}, headers, true)
	if err != nil {
		var known *KnownFailureError
		if errors.As(err, &known) {
			if rejectErr := r.store.Reject(ctx, owner, intentID, known); rejectErr != nil {
				return rejectErr
			}
		}
		return err
	}
	if len(response.Output) != 1 || response.Output[0].Type != ResponsesItemCompaction {
		failure := &KnownFailureError{Evidence: map[string]any{"reason": "compaction must produce exactly one compaction item and no tools", "response": response.Audit()}}
		if err := r.store.Reject(ctx, owner, intentID, failure); err != nil {
			return err
		}
		return failure
	}
	if response.Output[0].EncryptedContent == "" {
		failure := &KnownFailureError{Evidence: map[string]any{"reason": "compaction must include opaque encrypted_content", "response": response.Audit()}}
		if err := r.store.Reject(ctx, owner, intentID, failure); err != nil {
			return err
		}
		return failure
	}
	if contextBytes(response.Output) >= contextBytes(original.(*ResponsesContext).Items) {
		failure := &KnownFailureError{Evidence: map[string]any{"reason": "compaction did not reduce serialized model context", "response": response.Audit()}}
		if err := r.store.Reject(ctx, owner, intentID, failure); err != nil {
			return err
		}
		return failure
	}
	replacement := &ResponsesContext{Items: response.Output}
	session, err := r.store.GetSessionForConversation(ctx, owner)
	if err != nil {
		return err
	}
	environment, err := r.store.environmentContext(conversation, session)
	if err != nil {
		return err
	}
	environmentItem, err := newResponsesMessage("user", environmentContextUpdate(nil, environment))
	if err != nil {
		return err
	}
	replacement.Items = append([]ResponsesItem{environmentItem}, replacement.Items...)
	return r.store.FinishCompaction(ctx, owner, intentID, response.Audit(), original, replacement)
}

func contextBytes(items []ResponsesItem) int { data, _ := json.Marshal(items); return len(data) }
