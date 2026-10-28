package store

import (
	"database/sql"
	"errors"
	"fmt"

	model "github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/agentydragon/ducktape/agentplane/notification_service/storagepb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func encodeSubscriptionCreation(creation model.Subscribe) ([]byte, string, error) {
	if err := creation.Validate(); err != nil {
		return nil, "", err
	}
	stored := &storagepb.SubscriptionCreation{
		DestinationRef: &storagepb.DestinationRef{
			Namespace: creation.DestinationRef.Namespace,
			Name:      creation.DestinationRef.Name,
			Uid:       creation.DestinationRef.UID,
		},
		SessionId:    creation.SessionID,
		LifetimeDays: creation.LifetimeDays,
	}
	provider, err := subscriptionSourceProvider(creation.Source)
	if err != nil {
		return nil, "", err
	}
	switch source := creation.Source.(type) {
	case model.ActionsSource:
		stored.Source = &storagepb.SubscriptionCreation_Actions{Actions: &storagepb.ActionsSource{
			RequestId: source.RequestID[:], AfterSequence: source.AfterSequence,
		}}
	case model.GitHubSource:
		github := &storagepb.GitHubSource{Repository: source.Repository}
		switch subject := source.Subject.(type) {
		case model.PullRequestSubject:
			github.Subject = &storagepb.GitHubSource_PullRequest{PullRequest: &storagepb.PullRequest{Number: subject.Number}}
		case model.IssueSubject:
			github.Subject = &storagepb.GitHubSource_Issue{Issue: &storagepb.Issue{Number: subject.Number}}
		case model.BranchSubject:
			github.Subject = &storagepb.GitHubSource_Branch{Branch: &storagepb.Branch{Name: subject.Name}}
		case model.CommitSubject:
			github.Subject = &storagepb.GitHubSource_Commit{Commit: &storagepb.Commit{Sha: subject.SHA}}
		default:
			return nil, "", fmt.Errorf("unsupported GitHub subject %T", source.Subject)
		}
		for _, filter := range model.NormalizeFilters(source.Events) {
			github.Events = append(github.Events, &storagepb.EventFilter{Event: string(filter.Event), Actions: filter.Actions})
		}
		stored.Source = &storagepb.SubscriptionCreation_Github{Github: github}
	default:
		return nil, "", fmt.Errorf("unsupported subscription source %T", creation.Source)
	}
	encoded, err := proto.Marshal(stored)
	if err != nil {
		return nil, "", err
	}
	return encoded, provider, nil
}

func decodeSubscriptionCreation(data []byte, idempotencyKey, provider string) (model.Subscribe, error) {
	var stored storagepb.SubscriptionCreation
	if err := proto.Unmarshal(data, &stored); err != nil {
		return model.Subscribe{}, err
	}
	if stored.GetDestinationRef() == nil {
		return model.Subscribe{}, errors.New("destination_ref is required")
	}
	creation := model.Subscribe{
		DestinationRef: model.DestinationRef{
			Namespace: stored.GetDestinationRef().GetNamespace(),
			Name:      stored.GetDestinationRef().GetName(),
			UID:       stored.GetDestinationRef().GetUid(),
		},
		SessionID: stored.GetSessionId(), IdempotencyKey: idempotencyKey,
		LifetimeDays: stored.GetLifetimeDays(),
	}
	switch source := stored.GetSource().(type) {
	case *storagepb.SubscriptionCreation_Actions:
		if source.Actions == nil {
			return model.Subscribe{}, errors.New("actions source is required")
		}
		requestID, err := uuid.FromBytes(source.Actions.GetRequestId())
		if err != nil {
			return model.Subscribe{}, fmt.Errorf("actions request_id: %w", err)
		}
		creation.Source = model.ActionsSource{RequestID: requestID, AfterSequence: source.Actions.GetAfterSequence()}
	case *storagepb.SubscriptionCreation_Github:
		if source.Github == nil {
			return model.Subscribe{}, errors.New("GitHub source is required")
		}
		github := model.GitHubSource{Repository: source.Github.GetRepository()}
		switch subject := source.Github.GetSubject().(type) {
		case *storagepb.GitHubSource_PullRequest:
			if subject.PullRequest == nil {
				return model.Subscribe{}, errors.New("pull request subject is required")
			}
			github.Subject = model.PullRequestSubject{Number: subject.PullRequest.GetNumber()}
		case *storagepb.GitHubSource_Issue:
			if subject.Issue == nil {
				return model.Subscribe{}, errors.New("issue subject is required")
			}
			github.Subject = model.IssueSubject{Number: subject.Issue.GetNumber()}
		case *storagepb.GitHubSource_Branch:
			if subject.Branch == nil {
				return model.Subscribe{}, errors.New("branch subject is required")
			}
			github.Subject = model.BranchSubject{Name: subject.Branch.GetName()}
		case *storagepb.GitHubSource_Commit:
			if subject.Commit == nil {
				return model.Subscribe{}, errors.New("commit subject is required")
			}
			github.Subject = model.CommitSubject{SHA: subject.Commit.GetSha()}
		default:
			return model.Subscribe{}, errors.New("GitHub subject is required")
		}
		if len(source.Github.GetEvents()) > 0 {
			github.Events = make([]model.EventFilter, len(source.Github.GetEvents()))
			for i, filter := range source.Github.GetEvents() {
				github.Events[i] = model.EventFilter{Event: model.EventName(filter.GetEvent()), Actions: filter.GetActions()}
			}
		}
		creation.Source = github
	default:
		return model.Subscribe{}, errors.New("subscription source is required")
	}
	if err := creation.Validate(); err != nil {
		return model.Subscribe{}, err
	}
	storedProvider, err := subscriptionSourceProvider(creation.Source)
	if err != nil {
		return model.Subscribe{}, err
	}
	if storedProvider != provider {
		return model.Subscribe{}, fmt.Errorf("typed subscription source provider %q does not match stored discriminator %q", storedProvider, provider)
	}
	return creation, nil
}

func decodeStoredSubscriptionCreation(
	id uuid.UUID, typed sql.Null[[]byte], provider sql.NullString, idempotencyKey string,
) (model.Subscribe, error) {
	if typed.Valid != provider.Valid {
		return model.Subscribe{}, fmt.Errorf("subscription %s has incomplete typed creation fields", id)
	}
	if !typed.Valid {
		return model.Subscribe{}, fmt.Errorf("subscription %s has no typed creation", id)
	}
	creation, err := decodeSubscriptionCreation(typed.V, idempotencyKey, provider.String)
	if err != nil {
		return model.Subscribe{}, fmt.Errorf("subscription %s typed creation: %w", id, err)
	}
	return creation, nil
}

func subscriptionSourceProvider(source model.Source) (string, error) {
	switch source.(type) {
	case model.ActionsSource:
		return "actions", nil
	case model.GitHubSource:
		return "github", nil
	default:
		return "", fmt.Errorf("unsupported subscription source %T", source)
	}
}
