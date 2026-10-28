package store

import (
	"strconv"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

func TestAMessageIdentityIsReadBackWithItsSender(t *testing.T) {
	const wire = `{"provider":"message","message_id":"5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60","sender":{"namespace":"test-namespace","name":"test-sender"}}`
	want := model.MessageEvent{
		MessageID: uuid.MustParse("5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60"),
		Sender:    model.ServiceAccount{Namespace: "test-namespace", Name: "test-sender"},
	}
	encoded, err := marshalHistoricalEventIdentity(want)
	if err != nil {
		t.Fatal(err)
	}
	sameJSON(t, encoded, []byte(wire))
	got, err := decodeHistoricalEventIdentity([]byte(wire))
	if err != nil {
		t.Fatal(err)
	}
	if got != model.EventIdentity(want) {
		t.Fatalf("decoded historical identity = %#v, want %#v", got, want)
	}
	for name, malformed := range map[string]string{
		"no sender":     `{"provider":"message","message_id":"5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60"}`,
		"no message ID": `{"provider":"message","sender":{"namespace":"test-namespace","name":"test-sender"}}`,
	} {
		if _, err := decodeHistoricalEventIdentity([]byte(malformed)); err == nil {
			t.Errorf("%s: decoded", name)
		}
	}
}

func TestAMergeabilityObservationIdentityIsReadBackWithoutAWebhookDeliveryID(t *testing.T) {
	const wire = `{"provider":"github_mergeability","app_id":11,"repository_id":22,"pull_request_number":7,"sequence":3}`
	want := model.GitHubMergeabilityEvent{AppID: 11, RepositoryID: 22, PullRequestNumber: 7, Sequence: 3}
	encoded, err := marshalHistoricalEventIdentity(want)
	if err != nil {
		t.Fatal(err)
	}
	sameJSON(t, encoded, []byte(wire))
	got, err := decodeHistoricalEventIdentity([]byte(wire))
	if err != nil {
		t.Fatal(err)
	}
	if got != model.EventIdentity(want) {
		t.Fatalf("decoded historical identity = %#v, want %#v", got, want)
	}
	for name, malformed := range map[string]string{
		"no app ID":              `{"provider":"github_mergeability","repository_id":22,"pull_request_number":7,"sequence":3}`,
		"no repository ID":       `{"provider":"github_mergeability","app_id":11,"pull_request_number":7,"sequence":3}`,
		"no pull request number": `{"provider":"github_mergeability","app_id":11,"repository_id":22,"sequence":3}`,
		"no sequence":            `{"provider":"github_mergeability","app_id":11,"repository_id":22,"pull_request_number":7}`,
	} {
		if _, err := decodeHistoricalEventIdentity([]byte(malformed)); err == nil {
			t.Errorf("%s: decoded", name)
		}
	}
	webhook := `{"provider":"github","app_id":11,"delivery_id":"5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60","repository_id":22,"event":"mergeability_changed"}`
	if _, err := decodeHistoricalEventIdentity([]byte(webhook)); err == nil {
		t.Fatal("decoded an Agentplane observation as a GitHub webhook event")
	}
}

func TestAMergeabilitySnapshotIdentityUsesItsOwnProviderAndVersion(t *testing.T) {
	const wire = `{"provider":"github_mergeability_snapshot","app_id":11,"repository_id":22,"pull_request_number":7,"snapshot_version":4}`
	want := model.GitHubMergeabilitySnapshotEvent{AppID: 11, RepositoryID: 22, PullRequestNumber: 7, Version: 4}
	encoded, err := marshalHistoricalEventIdentity(want)
	if err != nil {
		t.Fatal(err)
	}
	sameJSON(t, encoded, []byte(wire))
	got, err := decodeHistoricalEventIdentity([]byte(wire))
	if err != nil {
		t.Fatal(err)
	}
	if got != model.EventIdentity(want) {
		t.Fatalf("decoded historical identity = %#v, want %#v", got, want)
	}
	missingVersion := `{"provider":"github_mergeability_snapshot","app_id":11,"repository_id":22,"pull_request_number":7}`
	if _, err := decodeHistoricalEventIdentity([]byte(missingVersion)); err == nil {
		t.Fatal("decoded a snapshot identity without its version")
	}
}

func TestMessageEventKeepsItsPermissiveSenderWireShape(t *testing.T) {
	for _, test := range []struct {
		name   string
		sender string
		want   model.ServiceAccount
	}{
		{name: "missing namespace", sender: `{"name":"test-sender"}`, want: model.ServiceAccount{Name: "test-sender"}},
		{name: "empty namespace", sender: `{"namespace":"","name":"test-sender"}`, want: model.ServiceAccount{Name: "test-sender"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			data := []byte(`{"provider":"message","message_id":"5b0a6c51-8d7e-4f0a-9d2b-7c3e1f4a2b60","sender":` + test.sender + `}`)
			got, err := decodeHistoricalEventIdentity(data)
			if err != nil {
				t.Fatal(err)
			}
			message, ok := got.(model.MessageEvent)
			if !ok {
				t.Fatalf("decoded identity = %#v, want a message identity", got)
			}
			if message.Sender != test.want {
				t.Errorf("sender = %+v, want %+v", message.Sender, test.want)
			}
		})
	}
}

func TestAGitHubObservationIdentityIsReadBackWithoutAWebhookDeliveryID(t *testing.T) {
	const wire = `{"provider":"github_observation","app_id":11,"repository_id":22,"event":"check_run","sequence":3}`
	want := model.GitHubObservationEvent{AppID: 11, RepositoryID: 22, Event: model.EventCheckRun, Sequence: 3}
	encoded, err := marshalHistoricalEventIdentity(want)
	if err != nil {
		t.Fatal(err)
	}
	sameJSON(t, encoded, []byte(wire))
	got, err := decodeHistoricalEventIdentity([]byte(wire))
	if err != nil {
		t.Fatal(err)
	}
	if got != model.EventIdentity(want) {
		t.Fatalf("decoded historical identity = %#v, want %#v", got, want)
	}
	for event, sequence := range map[model.EventName]int64{
		model.EventStatus: 4, model.EventPullRequestReview: 5, model.EventPullRequest: 6,
	} {
		fixture := []byte(`{"provider":"github_observation","app_id":11,"repository_id":22,"event":"` + string(event) + `","sequence":` + strconv.FormatInt(sequence, 10) + `}`)
		got, err := decodeHistoricalEventIdentity(fixture)
		if err != nil {
			t.Fatalf("decode %s observation: %v", event, err)
		}
		want := model.GitHubObservationEvent{AppID: 11, RepositoryID: 22, Event: event, Sequence: sequence}
		if got != model.EventIdentity(want) {
			t.Errorf("decoded %s identity = %#v, want %#v", event, got, want)
		}
	}
	for name, malformed := range map[string]string{
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
		if _, err := decodeHistoricalEventIdentity([]byte(malformed)); err == nil {
			t.Errorf("%s: decoded", name)
		}
	}
}

func TestHistoricalSubscriptionsAndSubjectsRemainReadable(t *testing.T) {
	for _, vector := range loadGolden(t).Subscriptions {
		t.Run(vector.Name, func(t *testing.T) {
			input, err := decodeHistoricalSubscription(vector.Input)
			if err != nil {
				t.Fatalf("decode historical input: %v", err)
			}
			stored, err := decodeHistoricalSubscription(vector.Stored)
			if err != nil {
				t.Fatalf("decode historical stored creation: %v", err)
			}
			if !stored.Equal(input) {
				t.Errorf("stored creation %+v differs from input %+v", stored, input)
			}
		})
	}
	for _, vector := range loadGolden(t).SubjectKeys {
		t.Run(vector.Kind+"/"+vector.Key, func(t *testing.T) {
			subject, err := decodeHistoricalSubject(vector.Subject)
			if err != nil {
				t.Fatal(err)
			}
			key, err := NewGitHubSubjectKey(7, subject)
			if err != nil {
				t.Fatal(err)
			}
			want := GitHubSubjectKey{RepositoryID: 7, Kind: vector.Kind, Key: vector.Key}
			if key != want {
				t.Errorf("key = %+v, want %+v", key, want)
			}
		})
	}
}
