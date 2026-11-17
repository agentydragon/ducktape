package sourceadapter

import (
	"testing"
	"time"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

func TestSummarizeEntryExposesOnlyStructuredLifecycleInformation(t *testing.T) {
	request := uuid.New()
	event := WrapActionsEvent(model.ActionsEvent{RequestID: request, Sequence: 3})
	createdAt := time.Date(2026, time.April, 5, 6, 7, 8, 9, time.FixedZone("local", 3*60*60))
	summary, err := SummarizeEntry(1, event, []byte(`{"state":"decision_pending","actor":"private"}`), createdAt)
	if err != nil {
		t.Fatal(err)
	}
	if summary.Cursor != 1 || !summary.CreatedAt.Equal(createdAt.UTC()) || summary.Event != event || summary.ActionState == nil || *summary.ActionState != "decision_pending" {
		t.Fatalf("Action summary = %+v", summary)
	}

	github := WrapGitHubEvent(githubmodel.WebhookEvent{Event: githubmodel.EventCheckRun})
	summary, err = SummarizeEntry(2, github, []byte(`{"check_run":{"conclusion":"success","output":"private"}}`), time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	if summary.Event != github || summary.GitHubConclusion == nil || *summary.GitHubConclusion != "success" {
		t.Fatalf("GitHub check run summary = %+v", summary)
	}

	workflow := WrapGitHubEvent(githubmodel.WebhookEvent{Event: githubmodel.EventWorkflowRun})
	summary, err = SummarizeEntry(3, workflow, []byte(`{"workflow_run":{"conclusion":"cancelled","logs_url":"private"}}`), time.Time{})
	if err != nil || summary.GitHubConclusion == nil || *summary.GitHubConclusion != "cancelled" {
		t.Fatalf("GitHub workflow run summary = %+v, %v", summary, err)
	}

	message := WrapMessageEvent(model.MessageEvent{MessageID: request, Sender: model.ServiceAccount{Namespace: "test-namespace", Name: "test-sender"}})
	summary, err = SummarizeEntry(4, message, []byte(`{"body":"private"}`), time.Time{})
	if err != nil || summary.Event != message || summary.ActionState != nil || summary.GitHubConclusion != nil {
		t.Fatalf("Message summary = %+v, %v", summary, err)
	}
}

func TestSummarizeEntryIgnoresUnexpectedLifecycleFieldShapes(t *testing.T) {
	actions := WrapActionsEvent(model.ActionsEvent{RequestID: uuid.New(), Sequence: 1})
	for _, payload := range []string{
		`{"state":17}`,
		`{"state":null}`,
		`{"state":[]}`,
	} {
		summary, err := SummarizeEntry(1, actions, []byte(payload), time.Time{})
		if err != nil || summary.ActionState != nil {
			t.Errorf("Action summary for %s = %+v, %v; want no lifecycle state", payload, summary, err)
		}
	}

	github := WrapGitHubEvent(githubmodel.WebhookEvent{Event: githubmodel.EventCheckRun})
	for _, payload := range []string{
		`{"check_run":null}`,
		`{"check_run":[]}`,
		`{"check_run":"private"}`,
		`{"check_run":{"conclusion":null}}`,
		`{"check_run":{"conclusion":17}}`,
		`{"check_run":{"conclusion":"private arbitrary text"}}`,
	} {
		summary, err := SummarizeEntry(2, github, []byte(payload), time.Time{})
		if err != nil || summary.GitHubConclusion != nil {
			t.Errorf("GitHub summary for %s = %+v, %v; want no lifecycle conclusion", payload, summary, err)
		}
	}
}

func TestSummarizeEntryRetainsRootJSONErrors(t *testing.T) {
	event := WrapActionsEvent(model.ActionsEvent{RequestID: uuid.New(), Sequence: 1})
	for _, payload := range []string{`not json`, `[`, `[]`} {
		if _, err := SummarizeEntry(4, event, []byte(payload), time.Time{}); err == nil {
			t.Errorf("SummarizeEntry accepted malformed root %q", payload)
		}
	}
	if summary, err := SummarizeEntry(5, event, []byte(`null`), time.Time{}); err != nil || summary.ActionState != nil {
		t.Fatalf("null root summary = %+v, %v; want no state", summary, err)
	}
}

func TestSummarizeStoredMessageDoesNotDecodeContentOrPayload(t *testing.T) {
	event := WrapMessageEvent(model.MessageEvent{MessageID: uuid.New(), Sender: model.ServiceAccount{Namespace: "sender", Name: "sandbox"}})
	createdAt := time.Date(2026, time.April, 5, 6, 7, 8, 9, time.FixedZone("local", -4*60*60))
	summary, err := SummarizeStoredEntry(8, event, []byte(`not json`), model.EntryContentStorage{}, createdAt)
	if err != nil {
		t.Fatal(err)
	}
	if summary.Cursor != 8 || !summary.CreatedAt.Equal(createdAt.UTC()) || summary.Event != event || summary.ActionState != nil || summary.GitHubConclusion != nil {
		t.Fatalf("stored Message summary = %+v", summary)
	}
}
