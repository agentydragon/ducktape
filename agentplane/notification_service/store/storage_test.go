package store

import (
	"fmt"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/google/uuid"
)

func TestSubscriptionCreationConfigRoundTripsTypedSourcesAndNormalizesFilters(t *testing.T) {
	base := model.Subscribe{
		DestinationRef: model.DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-uid"},
		SessionID:      "test-session", IdempotencyKey: "test-key", LifetimeDays: 7,
	}
	requestID := uuid.New()
	creations := []model.Subscribe{
		{
			DestinationRef: base.DestinationRef, SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: model.ActionsSource{RequestID: requestID, AfterSequence: 12},
		},
		{
			DestinationRef: base.DestinationRef, SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: model.GitHubSource{
				Repository: "test-owner/test-repo",
				Subject:    model.PullRequestSubject{Number: 7}, Events: []model.EventFilter{
					{Event: model.EventCheckRun, Actions: []string{"completed", "created", "completed"}},
					{Event: model.EventIssueComment, Actions: []string{"created"}},
					{Event: model.EventCheckRun, Actions: []string{"created", "completed"}},
				},
			},
		},
		{
			DestinationRef: base.DestinationRef, SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: model.GitHubSource{
				Repository: "test-owner/test-repo",
				Subject:    model.IssueSubject{Number: 8},
			},
		},
		{
			DestinationRef: base.DestinationRef, SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: model.GitHubSource{
				Repository: "test-owner/test-repo",
				Subject:    model.BranchSubject{Name: "test-branch"},
			},
		},
		{
			DestinationRef: base.DestinationRef, SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: model.GitHubSource{
				Repository: "test-owner/test-repo",
				Subject:    model.CommitSubject{SHA: "0123456789abcdef0123456789abcdef01234567"},
			},
		},
	}
	for i, creation := range creations {
		creation := creation
		t.Run(fmt.Sprintf("source-%d", i), func(t *testing.T) {
			if err := creation.Validate(); err != nil {
				t.Fatal(err)
			}
			first, provider, err := storagecodec.EncodeSubscriptionCreation(creation)
			if err != nil {
				t.Fatal(err)
			}
			decoded, err := storagecodec.DecodeSubscriptionCreation(first, creation.IdempotencyKey, provider)
			if err != nil {
				t.Fatal(err)
			}
			if !creation.Equal(decoded) {
				t.Fatalf("decoded creation %+v, want %+v", decoded, creation)
			}
		})
	}
}

func TestSubscriptionCreationConfigRejectsAbsentTypedFields(t *testing.T) {
	if _, err := storagecodec.DecodeSubscriptionCreation(nil, "test-key", "actions"); err == nil {
		t.Fatal("decoded absent typed creation fields")
	}
}

func TestSubscriptionCreationConfigChecksRelationalProvider(t *testing.T) {
	creation := model.Subscribe{
		DestinationRef: model.DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-uid"},
		SessionID:      "test-session", IdempotencyKey: "test-key", LifetimeDays: 7,
		Source: model.ActionsSource{RequestID: uuid.New()},
	}
	encoded, _, err := storagecodec.EncodeSubscriptionCreation(creation)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := storagecodec.DecodeSubscriptionCreation(encoded, creation.IdempotencyKey, "github"); err == nil {
		t.Fatal("accepted an Actions payload with the GitHub relational discriminator")
	}
}
