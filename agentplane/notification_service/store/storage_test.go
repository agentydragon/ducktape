package store

import (
	"fmt"
	"testing"

	"github.com/google/uuid"
)

func TestSubscriptionCreationConfigRoundTripsTypedSourcesAndNormalizesFilters(t *testing.T) {
	base := Subscribe{
		DestinationRef: DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-uid"},
		SessionID:      "test-session", IdempotencyKey: "test-key", LifetimeDays: 7,
	}
	requestID := uuid.New()
	creations := []Subscribe{
		{
			DestinationRef: base.DestinationRef, SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: ActionsSource{RequestID: requestID, AfterSequence: 12},
		},
		{
			DestinationRef: base.DestinationRef, SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: GitHubSource{
				Repository: "test-owner/test-repo",
				Subject:    PullRequestSubject{Number: 7}, Events: []EventFilter{
					{Event: EventCheckRun, Actions: []string{"completed", "created", "completed"}},
					{Event: EventIssueComment, Actions: []string{"created"}},
					{Event: EventCheckRun, Actions: []string{"created", "completed"}},
				},
			},
		},
		{
			DestinationRef: base.DestinationRef, SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: GitHubSource{
				Repository: "test-owner/test-repo",
				Subject:    IssueSubject{Number: 8},
			},
		},
		{
			DestinationRef: base.DestinationRef, SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: GitHubSource{
				Repository: "test-owner/test-repo",
				Subject:    BranchSubject{Name: "test-branch"},
			},
		},
		{
			DestinationRef: base.DestinationRef, SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: GitHubSource{
				Repository: "test-owner/test-repo",
				Subject:    CommitSubject{SHA: "0123456789abcdef0123456789abcdef01234567"},
			},
		},
	}
	for i, creation := range creations {
		creation := creation
		t.Run(fmt.Sprintf("source-%d", i), func(t *testing.T) {
			if err := creation.Validate(); err != nil {
				t.Fatal(err)
			}
			first, provider, err := encodeSubscriptionCreation(creation)
			if err != nil {
				t.Fatal(err)
			}
			decoded, err := decodeSubscriptionCreation(first, creation.IdempotencyKey, provider)
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
	if _, err := decodeSubscriptionCreation(nil, "test-key", "actions"); err == nil {
		t.Fatal("decoded absent typed creation fields")
	}
}

func TestSubscriptionCreationConfigChecksRelationalProvider(t *testing.T) {
	creation := Subscribe{
		DestinationRef: DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-uid"},
		SessionID:      "test-session", IdempotencyKey: "test-key", LifetimeDays: 7,
		Source: ActionsSource{RequestID: uuid.New()},
	}
	encoded, _, err := encodeSubscriptionCreation(creation)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := decodeSubscriptionCreation(encoded, creation.IdempotencyKey, "github"); err == nil {
		t.Fatal("accepted an Actions payload with the GitHub relational discriminator")
	}
}
