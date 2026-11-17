package sourceadapter

import (
	"encoding/json"
	"fmt"

	githubstoragecodec "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
)

// EntryContentStorageFor dispatches native entry content to the codec owned by its source.
func EntryContentStorageFor(event EventIdentity, payload json.RawMessage) (storagecodec.EntryContentStorage, error) {
	switch event := event.(type) {
	case ActionsEvent:
		return storagecodec.ActionEntryContentStorageFor(event.ActionsEvent, payload)
	case GitHubMergeabilityEvent:
		return githubstoragecodec.MergeabilityEntryContentStorageFor(payload, event.PullRequestNumber)
	case GitHubMergeabilitySnapshotEvent:
		return githubstoragecodec.MergeabilityEntryContentStorageFor(payload, event.PullRequestNumber)
	case MessageEvent:
		return storagecodec.MessageEntryContentStorageFor(payload)
	case GitHubEvent, GitHubObservationEvent:
		return storagecodec.EntryContentStorage{}, nil
	default:
		return storagecodec.EntryContentStorage{}, fmt.Errorf("unsupported event identity %T", event)
	}
}

// EntryPayloadFromRead reconstructs public payload bytes from the source's typed native columns.
func EntryPayloadFromRead(event EventIdentity, githubPayload []byte, content storagecodec.EntryContentStorage) (json.RawMessage, error) {
	switch event := event.(type) {
	case ActionsEvent:
		return content.ActionPayload(event.ActionsEvent)
	case GitHubEvent, GitHubObservationEvent:
		return githubstoragecodec.GitHubPayloadFromStorage(githubPayload)
	case GitHubMergeabilityEvent:
		return githubstoragecodec.MergeabilityPayloadFromStorage(content, event.PullRequestNumber)
	case GitHubMergeabilitySnapshotEvent:
		return githubstoragecodec.MergeabilityPayloadFromStorage(content, event.PullRequestNumber)
	case MessageEvent:
		return content.MessagePayload()
	default:
		return nil, fmt.Errorf("unsupported event identity %T", event)
	}
}
