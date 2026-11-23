package inbox

import (
	"fmt"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/google/uuid"
)

func TestSubscriptionCreationConfigRoundTripsTypedSourcesAndNormalizesFilters(t *testing.T) {
	base := sourceadapter.Subscribe{
		SessionID: "test-session", IdempotencyKey: "test-key", LifetimeDays: 7,
	}
	requestID := uuid.New()
	creations := []sourceadapter.Subscribe{
		{
			SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: sourceadapter.WrapActionsSource(model.ActionsSource{RequestID: requestID, AfterSequence: 12}),
		},
		{
			SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: sourceadapter.WrapGitHubSource(githubmodel.Source{
				Repository: "test-owner/test-repo",
				Subject:    githubmodel.PullRequestSubject{Number: 7}, Events: []githubmodel.EventFilter{
					{Event: githubmodel.EventCheckRun, Actions: []string{"completed", "created", "completed"}},
					{Event: githubmodel.EventIssueComment, Actions: []string{"created"}},
					{Event: githubmodel.EventCheckRun, Actions: []string{"created", "completed"}},
				},
			}),
		},
		{
			SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: sourceadapter.WrapGitHubSource(githubmodel.Source{
				Repository: "test-owner/test-repo",
				Subject:    githubmodel.IssueSubject{Number: 8},
			}),
		},
		{
			SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: sourceadapter.WrapGitHubSource(githubmodel.Source{
				Repository: "test-owner/test-repo",
				Subject:    githubmodel.BranchSubject{Name: "test-branch"},
			}),
		},
		{
			SessionID: base.SessionID, IdempotencyKey: base.IdempotencyKey,
			LifetimeDays: base.LifetimeDays, Source: sourceadapter.WrapGitHubSource(githubmodel.Source{
				Repository: "test-owner/test-repo",
				Subject:    githubmodel.CommitSubject{SHA: "0123456789abcdef0123456789abcdef01234567"},
			}),
		},
	}
	for i, creation := range creations {
		creation := creation
		t.Run(fmt.Sprintf("source-%d", i), func(t *testing.T) {
			if err := creation.Validate(); err != nil {
				t.Fatal(err)
			}
			first, provider, err := sourceadapter.EncodeSubscriptionCreation(creation)
			if err != nil {
				t.Fatal(err)
			}
			decoded, err := sourceadapter.DecodeSubscriptionCreation(first, creation.IdempotencyKey, provider)
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
	if _, err := sourceadapter.DecodeSubscriptionCreation(nil, "test-key", "actions"); err == nil {
		t.Fatal("decoded absent typed creation fields")
	}
}

func TestSubscriptionCreationConfigChecksRelationalProvider(t *testing.T) {
	creation := sourceadapter.Subscribe{
		SessionID: "test-session", IdempotencyKey: "test-key", LifetimeDays: 7,
		Source: sourceadapter.WrapActionsSource(model.ActionsSource{RequestID: uuid.New()}),
	}
	encoded, _, err := sourceadapter.EncodeSubscriptionCreation(creation)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := sourceadapter.DecodeSubscriptionCreation(encoded, creation.IdempotencyKey, "github"); err == nil {
		t.Fatal("accepted an Actions payload with the GitHub relational discriminator")
	}
}
