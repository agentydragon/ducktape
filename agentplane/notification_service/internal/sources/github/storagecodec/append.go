package storagecodec

import (
	"encoding/json"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model"
	commonmodel "github.com/agentydragon/ducktape/agentplane/notification_service/model" // Neutral storage values share a name with GitHub domain models.
)

func WebhookAppendInputFor(event model.WebhookEvent, payload json.RawMessage) (commonmodel.AppendInput, error) {
	if payload == nil {
		return commonmodel.AppendInput{}, errors.New("GitHub webhook content is absent")
	}
	var action commonmodel.EntryValue = commonmodel.EntryNull{}
	if event.Action != nil {
		action = commonmodel.EntryText(*event.Action)
	}
	return commonmodel.AppendInput{
		Key: commonmodel.EntryKey{Provider: commonmodel.EntryProviderGitHub, Fields: []commonmodel.EntryKeyField{
			{Column: commonmodel.EntryGitHubAppID, Value: commonmodel.EntryInteger(event.AppID)},
			{Column: commonmodel.EntryGitHubDeliveryID, Value: commonmodel.EntryUUID(event.DeliveryID)},
			{Column: commonmodel.EntryGitHubRepositoryID, Value: commonmodel.EntryInteger(event.RepositoryID)},
			{Column: commonmodel.EntryGitHubEvent, Value: commonmodel.EntryText(event.Event)},
			{Column: commonmodel.EntryGitHubAction, Value: action},
		}},
		Payload: commonmodel.EntryPayload{JSON: payload},
	}, nil
}

func ObservationAppendInputFor(event model.ObservationEvent, payload json.RawMessage) (commonmodel.AppendInput, error) {
	if payload == nil {
		return commonmodel.AppendInput{}, errors.New("GitHub observation content is absent")
	}
	return commonmodel.AppendInput{
		Key: commonmodel.EntryKey{Provider: commonmodel.EntryProviderGitHubObservation, Fields: []commonmodel.EntryKeyField{
			{Column: commonmodel.EntryGitHubAppID, Value: commonmodel.EntryInteger(event.AppID)},
			{Column: commonmodel.EntryGitHubRepositoryID, Value: commonmodel.EntryInteger(event.RepositoryID)},
			{Column: commonmodel.EntryGitHubEvent, Value: commonmodel.EntryText(event.Event)},
			{Column: commonmodel.EntryGitHubObservationSequence, Value: commonmodel.EntryInteger(event.Sequence)},
		}},
		Payload: commonmodel.EntryPayload{JSON: payload},
	}, nil
}

func MergeabilityAppendInputFor(event model.MergeabilityEvent, payload json.RawMessage) (commonmodel.AppendInput, error) {
	content, err := MergeabilityEntryContentStorageFor(payload, event.PullRequestNumber)
	if err != nil {
		return commonmodel.AppendInput{}, err
	}
	return commonmodel.AppendInput{
		Key: commonmodel.EntryKey{Provider: commonmodel.EntryProviderGitHubMergeability, Fields: []commonmodel.EntryKeyField{
			{Column: commonmodel.EntryGitHubAppID, Value: commonmodel.EntryInteger(event.AppID)},
			{Column: commonmodel.EntryGitHubRepositoryID, Value: commonmodel.EntryInteger(event.RepositoryID)},
			{Column: commonmodel.EntryGitHubPullRequestNumber, Value: commonmodel.EntryInteger(event.PullRequestNumber)},
			{Column: commonmodel.EntryGitHubMergeabilitySequence, Value: commonmodel.EntryInteger(event.Sequence)},
		}},
		Payload: commonmodel.EntryPayload{Content: content},
	}, nil
}

func MergeabilitySnapshotAppendInputFor(event model.MergeabilitySnapshotEvent, payload json.RawMessage) (commonmodel.AppendInput, error) {
	content, err := MergeabilityEntryContentStorageFor(payload, event.PullRequestNumber)
	if err != nil {
		return commonmodel.AppendInput{}, err
	}
	return commonmodel.AppendInput{
		Key: commonmodel.EntryKey{Provider: commonmodel.EntryProviderGitHubMergeability, Fields: []commonmodel.EntryKeyField{
			{Column: commonmodel.EntryGitHubAppID, Value: commonmodel.EntryInteger(event.AppID)},
			{Column: commonmodel.EntryGitHubRepositoryID, Value: commonmodel.EntryInteger(event.RepositoryID)},
			{Column: commonmodel.EntryGitHubPullRequestNumber, Value: commonmodel.EntryInteger(event.PullRequestNumber)},
			{Column: commonmodel.EntryGitHubSnapshotVersion, Value: commonmodel.EntryInteger(event.Version)},
		}},
		Payload: commonmodel.EntryPayload{Content: content},
	}, nil
}
