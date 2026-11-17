package storagecodec

import (
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
)

// EventIdentityFromStorage reconstructs the source-owned identity from existing relational columns.
func EventIdentityFromStorage(r storagecodec.EntryIdentityStorage) (model.EventIdentity, error) {
	switch r.Provider.String {
	case "github":
		appID, err := storagecodec.RequiredIdentityInt64("github_app_id", r.GitHubAppID)
		if err != nil {
			return nil, err
		}
		deliveryID, err := storagecodec.RequiredIdentityUUID("github_delivery_id", r.GitHubDeliveryID)
		if err != nil {
			return nil, err
		}
		repositoryID, err := storagecodec.RequiredIdentityInt64("github_repository_id", r.GitHubRepositoryID)
		if err != nil {
			return nil, err
		}
		if !r.GitHubEvent.Valid {
			return nil, errors.New("entry has no github_event")
		}
		event := model.EventName(r.GitHubEvent.String)
		if !model.IsWebhookEvent(event) {
			return nil, fmt.Errorf("unknown GitHub event %q", event)
		}
		var action *string
		if r.GitHubAction.Valid {
			action = &r.GitHubAction.String
		}
		return model.WebhookEvent{
			AppID: appID, DeliveryID: deliveryID, RepositoryID: repositoryID,
			Event: event, Action: action,
		}, nil
	case "github_observation":
		appID, err := storagecodec.RequiredIdentityInt64("github_app_id", r.GitHubAppID)
		if err != nil {
			return nil, err
		}
		repositoryID, err := storagecodec.RequiredIdentityInt64("github_repository_id", r.GitHubRepositoryID)
		if err != nil {
			return nil, err
		}
		if !r.GitHubEvent.Valid {
			return nil, errors.New("entry has no github_event")
		}
		event := model.EventName(r.GitHubEvent.String)
		if !model.IsWebhookEvent(event) {
			return nil, fmt.Errorf("unknown GitHub observation event %q", event)
		}
		sequence, err := storagecodec.RequiredIdentityInt64("github_observation_sequence", r.GitHubObservationSequence)
		if err != nil {
			return nil, err
		}
		return model.ObservationEvent{AppID: appID, RepositoryID: repositoryID, Event: event, Sequence: sequence}, nil
	case "github_mergeability":
		appID, err := storagecodec.RequiredIdentityInt64("github_app_id", r.GitHubAppID)
		if err != nil {
			return nil, err
		}
		repositoryID, err := storagecodec.RequiredIdentityInt64("github_repository_id", r.GitHubRepositoryID)
		if err != nil {
			return nil, err
		}
		pullRequestNumber, err := storagecodec.RequiredIdentityInt64("github_pull_request_number", r.GitHubPullRequestNumber)
		if err != nil {
			return nil, err
		}
		switch {
		case r.GitHubMergeabilitySequence.Valid && !r.GitHubSnapshotVersion.Valid:
			return model.MergeabilityEvent{
				AppID: appID, RepositoryID: repositoryID, PullRequestNumber: pullRequestNumber,
				Sequence: r.GitHubMergeabilitySequence.Int64,
			}, nil
		case !r.GitHubMergeabilitySequence.Valid && r.GitHubSnapshotVersion.Valid:
			return model.MergeabilitySnapshotEvent{
				AppID: appID, RepositoryID: repositoryID, PullRequestNumber: pullRequestNumber,
				Version: r.GitHubSnapshotVersion.Int64,
			}, nil
		default:
			return nil, errors.New("entry must have exactly one mergeability sequence or snapshot version")
		}
	default:
		return nil, fmt.Errorf("unknown GitHub event identity provider %q", r.Provider.String)
	}
}
