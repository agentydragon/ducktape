package store

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/google/uuid"
)

func TestAMessageIdentityIsReadBackWithItsSender(t *testing.T) {
	sent := MessageEvent{
		MessageID: uuid.MustParse("5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60"),
		Sender:    ServiceAccount{Namespace: "test-namespace", Name: "test-sender"},
	}
	stored, err := json.Marshal(sent)
	if err != nil {
		t.Fatal(err)
	}
	read, err := DecodeEventIdentity(stored)
	if err != nil {
		t.Fatal(err)
	}
	requireEqual(t, read, EventIdentity(sent))
	for name, stored := range map[string]string{
		"no sender":     `{"provider": "message", "message_id": "5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60"}`,
		"no message ID": `{"provider": "message", "sender": {"namespace": "test-namespace", "name": "test-sender"}}`,
	} {
		if _, err := DecodeEventIdentity([]byte(stored)); err == nil {
			t.Errorf("%s: decoded", name)
		}
	}
}

func TestAMergeabilityObservationIdentityIsReadBackWithoutAWebhookDeliveryID(t *testing.T) {
	sent := GitHubMergeabilityEvent{AppID: 11, RepositoryID: 22, PullRequestNumber: 7, Sequence: 3}
	stored, err := json.Marshal(sent)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"provider":"github_mergeability","app_id":11,"repository_id":22,"pull_request_number":7,"sequence":3}`
	requireEqual(t, string(stored), want)
	read, err := DecodeEventIdentity(stored)
	if err != nil {
		t.Fatal(err)
	}
	requireEqual(t, read, EventIdentity(sent))
	for name, stored := range map[string]string{
		"no app ID":              `{"provider":"github_mergeability","repository_id":22,"pull_request_number":7,"sequence":3}`,
		"no repository ID":       `{"provider":"github_mergeability","app_id":11,"pull_request_number":7,"sequence":3}`,
		"no pull request number": `{"provider":"github_mergeability","app_id":11,"repository_id":22,"sequence":3}`,
		"no sequence":            `{"provider":"github_mergeability","app_id":11,"repository_id":22,"pull_request_number":7}`,
	} {
		if _, err := DecodeEventIdentity([]byte(stored)); err == nil {
			t.Errorf("%s: decoded", name)
		}
	}
	webhook := `{"provider":"github","app_id":11,"delivery_id":"5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60","repository_id":22,"event":"mergeability_changed"}`
	if _, err := DecodeEventIdentity([]byte(webhook)); err == nil {
		t.Fatal("decoded an Agentplane observation as a GitHub webhook event")
	}
}

func TestAMergeabilitySnapshotIdentityUsesItsOwnProviderAndVersion(t *testing.T) {
	sent := GitHubMergeabilitySnapshotEvent{AppID: 11, RepositoryID: 22, PullRequestNumber: 7, Version: 4}
	stored, err := json.Marshal(sent)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"provider":"github_mergeability_snapshot","app_id":11,"repository_id":22,"pull_request_number":7,"snapshot_version":4}`
	requireEqual(t, string(stored), want)
	read, err := DecodeEventIdentity(stored)
	if err != nil {
		t.Fatal(err)
	}
	requireEqual(t, read, EventIdentity(sent))
	if _, err := DecodeEventIdentity([]byte(`{"provider":"github_mergeability_snapshot","app_id":11,"repository_id":22,"pull_request_number":7}`)); err == nil {
		t.Fatal("decoded a snapshot identity without its version")
	}
}

func TestMergeabilityChangedIsAPullRequestOnlyActionlessSelector(t *testing.T) {
	pr := GitHubSource{Repository: "test-owner/test-repo", Subject: PullRequestSubject{Number: 7}}
	for _, subject := range []GitHubSubject{PullRequestSubject{Number: 7}} {
		source := GitHubSource{Repository: pr.Repository, Subject: subject, Events: []EventFilter{{Event: EventMergeabilityChanged}}}
		if err := source.validate(); err != nil {
			t.Fatalf("pull request selector: %v", err)
		}
	}
	for _, actions := range [][]string{{"opened"}, {}} {
		source := GitHubSource{Repository: pr.Repository, Subject: pr.Subject, Events: []EventFilter{{Event: EventMergeabilityChanged, Actions: actions}}}
		if err := source.validate(); err == nil {
			t.Errorf("selector with actions %v was accepted", actions)
		}
	}
	for _, subject := range []GitHubSubject{
		IssueSubject{Number: 7}, BranchSubject{Name: "test-branch"}, CommitSubject{SHA: strings.Repeat("a", 40)},
	} {
		source := GitHubSource{Repository: pr.Repository, Subject: subject, Events: []EventFilter{{Event: EventMergeabilityChanged}}}
		if err := source.validate(); err == nil {
			t.Errorf("selector was accepted for %T", subject)
		}
	}
	found := false
	for _, filter := range pr.Filters() {
		found = found || filter.Event == EventMergeabilityChanged
	}
	if !found {
		t.Fatal("default pull-request events omit mergeability_changed")
	}
}

func TestMessageEventKeepsItsPermissiveSenderWireShape(t *testing.T) {
	for _, test := range []struct {
		name   string
		sender string
		want   ServiceAccount
	}{
		{name: "missing namespace", sender: `{"name":"test-sender"}`, want: ServiceAccount{Name: "test-sender"}},
		{name: "empty namespace", sender: `{"namespace":"","name":"test-sender"}`, want: ServiceAccount{Name: "test-sender"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			data := []byte(`{"provider":"message","message_id":"5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60","sender":` + test.sender + `}`)
			got, err := DecodeEventIdentity(data)
			if err != nil {
				t.Fatal(err)
			}
			message := got.(MessageEvent)
			if message.Sender != test.want {
				t.Errorf("sender = %+v, want %+v", message.Sender, test.want)
			}
		})
	}
}

func TestAGitHubObservationIdentityIsReadBackWithoutAWebhookDeliveryID(t *testing.T) {
	sent := GitHubObservationEvent{AppID: 11, RepositoryID: 22, Event: EventCheckRun, Sequence: 3}
	stored, err := json.Marshal(sent)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"provider":"github_observation","app_id":11,"repository_id":22,"event":"check_run","sequence":3}`
	requireEqual(t, string(stored), want)
	read, err := DecodeEventIdentity(stored)
	if err != nil {
		t.Fatal(err)
	}
	requireEqual(t, read, EventIdentity(sent))
	status, err := DecodeEventIdentity([]byte(`{"provider":"github_observation","app_id":11,"repository_id":22,"event":"status","sequence":4}`))
	if err != nil {
		t.Fatal(err)
	}
	requireEqual(t, status, EventIdentity(GitHubObservationEvent{AppID: 11, RepositoryID: 22, Event: EventStatus, Sequence: 4}))
	for _, event := range []EventName{EventPullRequestReview, EventPullRequest} {
		want := GitHubObservationEvent{AppID: 11, RepositoryID: 22, Event: event, Sequence: 5}
		encoded, err := json.Marshal(want)
		if err != nil {
			t.Fatal(err)
		}
		decoded, err := DecodeEventIdentity(encoded)
		if err != nil {
			t.Fatal(err)
		}
		requireEqual(t, decoded, EventIdentity(want))
	}
	for name, stored := range map[string]string{
		"no app ID":         `{"provider":"github_observation","repository_id":22,"event":"check_run","sequence":3}`,
		"no repository ID":  `{"provider":"github_observation","app_id":11,"event":"check_run","sequence":3}`,
		"no event":          `{"provider":"github_observation","app_id":11,"repository_id":22,"sequence":3}`,
		"no sequence":       `{"provider":"github_observation","app_id":11,"repository_id":22,"event":"check_run"}`,
		"zero app ID":       `{"provider":"github_observation","app_id":0,"repository_id":22,"event":"check_run","sequence":3}`,
		"negative repo ID":  `{"provider":"github_observation","app_id":11,"repository_id":-1,"event":"check_run","sequence":3}`,
		"zero sequence":     `{"provider":"github_observation","app_id":11,"repository_id":22,"event":"check_run","sequence":0}`,
		"unsupported event": `{"provider":"github_observation","app_id":11,"repository_id":22,"event":"issues","sequence":3}`,
		"unknown field":     `{"provider":"github_observation","app_id":11,"repository_id":22,"event":"check_run","sequence":3,"delivery_id":"5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60"}`,
	} {
		if _, err := DecodeEventIdentity([]byte(stored)); err == nil {
			t.Errorf("%s: decoded", name)
		}
	}
}
