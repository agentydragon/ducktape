package sourceadapter

import (
	"database/sql"
	"errors"
	"fmt"

	githubstoragecodec "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/storagepb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

// EncodeSubscriptionCreation writes the existing typed persistence contract at the source boundary.
func EncodeSubscriptionCreation(creation Subscribe) ([]byte, string, error) {
	if err := creation.Validate(); err != nil {
		return nil, "", err
	}
	stored := &storagepb.SubscriptionCreation{SessionId: creation.SessionID, LifetimeDays: creation.LifetimeDays}
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
		github, err := githubstoragecodec.EncodeSubscriptionSource(source.Source)
		if err != nil {
			return nil, "", err
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

// DecodeSubscriptionCreation restores and validates the existing typed persistence contract.
func DecodeSubscriptionCreation(data []byte, idempotencyKey, provider string) (Subscribe, error) {
	var stored storagepb.SubscriptionCreation
	if err := proto.Unmarshal(data, &stored); err != nil {
		return Subscribe{}, err
	}
	creation := Subscribe{
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
		creation.Source = WrapActionsSource(model.ActionsSource{RequestID: requestID, AfterSequence: source.Actions.GetAfterSequence()})
	case *storagepb.SubscriptionCreation_Github:
		github, err := githubstoragecodec.DecodeSubscriptionSource(source.Github)
		if err != nil {
			return Subscribe{}, err
		}
		creation.Source = WrapGitHubSource(github)
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

// DecodeStoredSubscriptionCreation checks the relational discriminator against the typed protobuf.
func DecodeStoredSubscriptionCreation(
	id uuid.UUID, typed sql.Null[[]byte], provider sql.NullString, idempotencyKey string,
) (Subscribe, error) {
	if typed.Valid != provider.Valid {
		return Subscribe{}, fmt.Errorf("subscription %s has incomplete typed creation fields", id)
	}
	if !typed.Valid {
		return Subscribe{}, fmt.Errorf("subscription %s has no typed creation", id)
	}
	creation, err := DecodeSubscriptionCreation(typed.V, idempotencyKey, provider.String)
	if err != nil {
		return Subscribe{}, fmt.Errorf("subscription %s typed creation: %w", id, err)
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
