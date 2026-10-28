package store

import (
	"database/sql"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/notification_service/storagepb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func encodeSubscriptionCreation(creation Subscribe) ([]byte, string, error) {
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
	case ActionsSource:
		stored.Source = &storagepb.SubscriptionCreation_Actions{Actions: &storagepb.ActionsSource{
			RequestId: source.RequestID[:], AfterSequence: source.AfterSequence,
		}}
	case GitHubSource:
		github := &storagepb.GitHubSource{Repository: source.Repository}
		switch subject := source.Subject.(type) {
		case PullRequestSubject:
			github.Subject = &storagepb.GitHubSource_PullRequest{PullRequest: &storagepb.PullRequest{Number: subject.Number}}
		case IssueSubject:
			github.Subject = &storagepb.GitHubSource_Issue{Issue: &storagepb.Issue{Number: subject.Number}}
		case BranchSubject:
			github.Subject = &storagepb.GitHubSource_Branch{Branch: &storagepb.Branch{Name: subject.Name}}
		case CommitSubject:
			github.Subject = &storagepb.GitHubSource_Commit{Commit: &storagepb.Commit{Sha: subject.SHA}}
		default:
			return nil, "", fmt.Errorf("unsupported GitHub subject %T", source.Subject)
		}
		for _, filter := range normalizeFilters(source.Events) {
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

func decodeSubscriptionCreation(data []byte, idempotencyKey, provider string) (Subscribe, error) {
	var stored storagepb.SubscriptionCreation
	if err := proto.Unmarshal(data, &stored); err != nil {
		return Subscribe{}, err
	}
	if stored.GetDestinationRef() == nil {
		return Subscribe{}, errors.New("destination_ref is required")
	}
	creation := Subscribe{
		DestinationRef: DestinationRef{
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
			return Subscribe{}, errors.New("actions source is required")
		}
		requestID, err := uuid.FromBytes(source.Actions.GetRequestId())
		if err != nil {
			return Subscribe{}, fmt.Errorf("actions request_id: %w", err)
		}
		creation.Source = ActionsSource{RequestID: requestID, AfterSequence: source.Actions.GetAfterSequence()}
	case *storagepb.SubscriptionCreation_Github:
		if source.Github == nil {
			return Subscribe{}, errors.New("GitHub source is required")
		}
		github := GitHubSource{Repository: source.Github.GetRepository()}
		switch subject := source.Github.GetSubject().(type) {
		case *storagepb.GitHubSource_PullRequest:
			if subject.PullRequest == nil {
				return Subscribe{}, errors.New("pull request subject is required")
			}
			github.Subject = PullRequestSubject{Number: subject.PullRequest.GetNumber()}
		case *storagepb.GitHubSource_Issue:
			if subject.Issue == nil {
				return Subscribe{}, errors.New("issue subject is required")
			}
			github.Subject = IssueSubject{Number: subject.Issue.GetNumber()}
		case *storagepb.GitHubSource_Branch:
			if subject.Branch == nil {
				return Subscribe{}, errors.New("branch subject is required")
			}
			github.Subject = BranchSubject{Name: subject.Branch.GetName()}
		case *storagepb.GitHubSource_Commit:
			if subject.Commit == nil {
				return Subscribe{}, errors.New("commit subject is required")
			}
			github.Subject = CommitSubject{SHA: subject.Commit.GetSha()}
		default:
			return Subscribe{}, errors.New("GitHub subject is required")
		}
		if len(source.Github.GetEvents()) > 0 {
			github.Events = make([]EventFilter, len(source.Github.GetEvents()))
			for i, filter := range source.Github.GetEvents() {
				github.Events[i] = EventFilter{Event: EventName(filter.GetEvent()), Actions: filter.GetActions()}
			}
		}
		creation.Source = github
	default:
		return Subscribe{}, errors.New("subscription source is required")
	}
	if err := creation.Validate(); err != nil {
		return Subscribe{}, err
	}
	storedProvider, err := subscriptionSourceProvider(creation.Source)
	if err != nil {
		return Subscribe{}, err
	}
	if storedProvider != provider {
		return Subscribe{}, fmt.Errorf("typed subscription source provider %q does not match stored discriminator %q", storedProvider, provider)
	}
	return creation, nil
}

func decodeStoredSubscriptionCreation(
	id uuid.UUID, legacy []byte, typed sql.Null[[]byte], provider sql.NullString, idempotencyKey string,
) (Subscribe, error) {
	if typed.Valid != provider.Valid {
		return Subscribe{}, fmt.Errorf("subscription %s has incomplete typed creation fields", id)
	}
	if typed.Valid {
		creation, err := decodeSubscriptionCreation(typed.V, idempotencyKey, provider.String)
		if err != nil {
			return Subscribe{}, fmt.Errorf("subscription %s typed creation: %w", id, err)
		}
		return creation, nil
	}
	if legacy == nil {
		return Subscribe{}, fmt.Errorf("subscription %s has no stored creation", id)
	}
	var creation Subscribe
	if err := decode(legacy, &creation); err != nil {
		return Subscribe{}, fmt.Errorf("subscription %s creation: %w", id, err)
	}
	return creation, nil
}

func subscriptionSourceProvider(source Source) (string, error) {
	switch source.(type) {
	case ActionsSource:
		return "actions", nil
	case GitHubSource:
		return "github", nil
	default:
		return "", fmt.Errorf("unsupported subscription source %T", source)
	}
}
