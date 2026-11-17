package sourceadapter

import (
	"errors"
	"fmt"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model"               // GitHub and common notification models share a package name.
	githubstoragecodec "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/storagecodec" // GitHub and common storage codecs share a package name.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// EventIdentityFromStorage explicitly routes relational identities to their owning codec.
func EventIdentityFromStorage(row storagecodec.EntryIdentityStorage) (EventIdentity, error) {
	if !row.Provider.Valid {
		return nil, errors.New("entry has no relational event identity")
	}
	switch model.EntryProvider(row.Provider.String) {
	case model.EntryProviderActions:
		event, err := storagecodec.ActionsIdentityFromStorage(row)
		if err != nil {
			return nil, err
		}
		return WrapActionsEvent(event), nil
	case model.EntryProviderMessage:
		event, err := storagecodec.MessageIdentityFromStorage(row)
		if err != nil {
			return nil, err
		}
		return WrapMessageEvent(event), nil
	case model.EntryProviderGitHub, model.EntryProviderGitHubObservation, model.EntryProviderGitHubMergeability:
		event, err := githubstoragecodec.EventIdentityFromStorage(row)
		if err != nil {
			return nil, err
		}
		switch event := event.(type) {
		case githubmodel.WebhookEvent:
			return WrapGitHubEvent(event), nil
		case githubmodel.ObservationEvent:
			return WrapGitHubObservationEvent(event), nil
		case githubmodel.MergeabilityEvent:
			return WrapGitHubMergeabilityEvent(event), nil
		case githubmodel.MergeabilitySnapshotEvent:
			return WrapGitHubMergeabilitySnapshotEvent(event), nil
		default:
			return nil, fmt.Errorf("unknown GitHub event identity %T", event)
		}
	default:
		return nil, fmt.Errorf("unknown relational event identity provider %q", row.Provider.String)
	}
}
