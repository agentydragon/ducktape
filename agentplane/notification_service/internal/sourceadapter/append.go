package sourceadapter

import (
	"encoding/json"
	"fmt"

	githubstoragecodec "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/storagecodec" // GitHub and common storage codecs share a package name.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// AppendInputFor constructs a neutral append at the explicit source boundary.
func AppendInputFor(event EventIdentity, payload json.RawMessage) (model.AppendInput, error) {
	switch event := event.(type) {
	case ActionsEvent:
		content, err := storagecodec.ActionEntryContentStorageFor(event.ActionsEvent, payload)
		if err != nil {
			return model.AppendInput{}, err
		}
		return model.AppendInput{
			Key: model.EntryKey{Provider: model.EntryProviderActions, Fields: []model.EntryKeyField{
				{Column: model.EntryActionsRequestID, Value: model.EntryUUID(event.RequestID)},
				{Column: model.EntryActionsSequence, Value: model.EntryInteger(event.Sequence)},
			}},
			Payload: model.EntryPayload{Content: content},
		}, nil
	case MessageEvent:
		content, err := storagecodec.MessageEntryContentStorageFor(payload)
		if err != nil {
			return model.AppendInput{}, err
		}
		return model.AppendInput{
			Key: model.EntryKey{Provider: model.EntryProviderMessage, Fields: []model.EntryKeyField{
				{Column: model.EntryMessageID, Value: model.EntryUUID(event.MessageID)},
				{Column: model.EntryMessageSenderNamespace, Value: model.EntryText(event.Sender.Namespace)},
				{Column: model.EntryMessageSenderName, Value: model.EntryText(event.Sender.Name)},
			}},
			Payload: model.EntryPayload{Content: content, SenderIdentity: event.SenderIdentity},
		}, nil
	case GitHubEvent:
		return githubstoragecodec.WebhookAppendInputFor(event.WebhookEvent, payload)
	case GitHubObservationEvent:
		return githubstoragecodec.ObservationAppendInputFor(event.ObservationEvent, payload)
	case GitHubMergeabilityEvent:
		return githubstoragecodec.MergeabilityAppendInputFor(event.MergeabilityEvent, payload)
	case GitHubMergeabilitySnapshotEvent:
		return githubstoragecodec.MergeabilitySnapshotAppendInputFor(event.MergeabilitySnapshotEvent, payload)
	default:
		return model.AppendInput{}, fmt.Errorf("unsupported event identity %T", event)
	}
}
