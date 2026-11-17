package sourceadapter

import (
	"encoding/json"
	"fmt"

	githubstoragecodec "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// EntryPayloadFromRead reconstructs public payload bytes from the source's typed native columns.
func EntryPayloadFromRead(event EventIdentity, githubPayload []byte, content model.EntryContentStorage) (json.RawMessage, error) {
	switch event := event.(type) {
	case ActionsEvent:
		return storagecodec.ActionPayload(content, event.ActionsEvent)
	case GitHubEvent, GitHubObservationEvent:
		return githubstoragecodec.PayloadFromStorage(githubPayload)
	case GitHubMergeabilityEvent:
		return githubstoragecodec.MergeabilityPayloadFromStorage(content, event.PullRequestNumber)
	case GitHubMergeabilitySnapshotEvent:
		return githubstoragecodec.MergeabilityPayloadFromStorage(content, event.PullRequestNumber)
	case MessageEvent:
		return storagecodec.MessagePayload(content)
	default:
		return nil, fmt.Errorf("unsupported event identity %T", event)
	}
}
