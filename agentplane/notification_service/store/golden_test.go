package store

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/proto"
)

func TestDestinationKeysAreThosePythonComputes(t *testing.T) {
	for _, vector := range loadGolden(t).DestinationKeys {
		t.Run(vector.Name, func(t *testing.T) {
			if got := DestinationKey(vector.DestinationRef, vector.SessionID); got != vector.Key {
				t.Errorf("DestinationKey = %s, want %s", got, vector.Key)
			}
		})
	}
}

func TestCreationsAreReadAndWrittenAsPythonDoes(t *testing.T) {
	for _, vector := range loadGolden(t).Subscriptions {
		t.Run(vector.Name, func(t *testing.T) {
			var sent, stored Subscribe
			if err := decode(vector.Input, &sent); err != nil {
				t.Fatalf("decoding the input: %v", err)
			}
			if err := sent.Validate(); err != nil {
				t.Fatalf("Validate: %v", err)
			}
			written, err := json.Marshal(sent)
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, written, vector.Stored)

			// What Python stored reads back, without loss, as the creation that was sent.
			if err := decode(vector.Stored, &stored); err != nil {
				t.Fatalf("decoding the stored form: %v", err)
			}
			if !stored.Equal(sent) {
				t.Errorf("stored form %+v is not the creation sent %+v", stored, sent)
			}
			rewritten, err := json.Marshal(stored)
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, rewritten, vector.Stored)
		})
	}
}

func TestCreationsPythonRefusesAreRefused(t *testing.T) {
	for _, vector := range loadGolden(t).InvalidSubscriptions {
		t.Run(vector.Name, func(t *testing.T) {
			var creation Subscribe
			err := decode(vector.Input, &creation)
			if err == nil {
				err = creation.Validate()
				if err != nil && !errors.Is(err, ErrInvalid) {
					t.Errorf("Validate error %v is not ErrInvalid", err)
				}
			}
			if err == nil {
				t.Errorf("accepted %s", vector.Input)
			}
		})
	}
}

func TestCreationsAreTheSameWhereThePythonCheckSaysSo(t *testing.T) {
	for _, vector := range loadGolden(t).Comparisons {
		t.Run(vector.Name, func(t *testing.T) {
			var a, b Subscribe
			if err := errors.Join(decode(vector.A, &a), decode(vector.B, &b)); err != nil {
				t.Fatal(err)
			}
			if got := a.Equal(b); got != vector.Same {
				t.Errorf("a.Equal(b) = %v, want %v", got, vector.Same)
			}
			if got := b.Equal(a); got != vector.Same {
				t.Errorf("b.Equal(a) = %v, want %v", got, vector.Same)
			}
		})
	}
}

func TestActionEventsAreStoredAsPythonStoresThem(t *testing.T) {
	for _, vector := range loadGolden(t).ActionEvents {
		t.Run(vector.Name, func(t *testing.T) {
			var event ActionEvent
			if err := decode(vector.Wire, &event); err != nil {
				t.Fatal(err)
			}
			payload, err := json.Marshal(event)
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, payload, vector.Payload)
		})
	}
}

func TestActionEventsPythonRefusesAreRefused(t *testing.T) {
	for _, vector := range loadGolden(t).InvalidActionEvents {
		t.Run(vector.Name, func(t *testing.T) {
			var event ActionEvent
			if err := decode(vector.Wire, &event); err == nil {
				t.Errorf("accepted %s", vector.Wire)
			}
		})
	}
}

func TestActionActorDecoderKeepsExplicitNullCompatibility(t *testing.T) {
	for _, test := range []struct {
		name string
		json string
		want *action_servicepb.Principal
	}{
		{
			name: "caller with null operator fields",
			json: `{"account":{"namespace":"test-namespace","name":"test-caller"},"issuer":null,"subject":null}`,
			want: &action_servicepb.Principal{Principal: &action_servicepb.Principal_Caller{Caller: action.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"}.Proto()}},
		},
		{
			name: "operator with null account",
			json: `{"account":null,"issuer":"test-issuer","subject":"test-subject"}`,
			want: &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{Issuer: "test-issuer", Subject: "test-subject"}}},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			actor, err := decodeActionActor(json.RawMessage(test.json))
			if err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(actor, test.want) {
				t.Fatalf("actor = %#v, want %#v", actor, test.want)
			}
			written, err := action.EncodeStoredActor(actor)
			if err != nil {
				t.Fatal(err)
			}
			switch {
			case test.want.GetCaller() != nil:
				sameJSON(t, written, []byte(`{"account":{"namespace":"test-namespace","name":"test-caller"}}`))
			case test.want.GetOperator() != nil:
				sameJSON(t, written, []byte(`{"issuer":"test-issuer","subject":"test-subject"}`))
			}
		})
	}
}

func TestNoticeTextsAreThosePythonPrepares(t *testing.T) {
	for _, vector := range loadGolden(t).Notices {
		t.Run(vector.Name, func(t *testing.T) {
			if got := noticeText(vector.InboxID, vector.Acknowledged, vector.Through); got != vector.Text {
				t.Errorf("noticeText = %s, want %s", got, vector.Text)
			}
		})
	}
}

// A replayed boundary is compared as bytes, so the bytes Go stores and the bytes it makes of a History entry
// must be the ones Python makes of the same entry, in either direction.
func TestRunnerEntriesSerializeAsPythonDoes(t *testing.T) {
	for _, vector := range loadGolden(t).RunnerEntries {
		t.Run(vector.Name, func(t *testing.T) {
			want, err := hex.DecodeString(vector.Serialized)
			if err != nil {
				t.Fatal(err)
			}
			got, err := marshalEntry(runnerEntry(t, vector.Entry))
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(got, want) {
				t.Errorf("a JSON entry serializes as %x, want %x", got, want)
			}

			var read eventlogpb.EventEntry
			if err := proto.Unmarshal(want, &read); err != nil {
				t.Fatal(err)
			}
			again, err := marshalEntry(&read)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(again, want) {
				t.Errorf("Python's bytes serialize again as %x, want %x", again, want)
			}
		})
	}
}

func TestEventsAreReadAndWrittenAsPythonDoes(t *testing.T) {
	for _, vector := range loadGolden(t).Events {
		t.Run(vector.Name, func(t *testing.T) {
			event, err := DecodeEventIdentity(vector.Event)
			if err != nil {
				t.Fatal(err)
			}
			written, err := json.Marshal(event)
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, written, vector.Event)
		})
	}
}

func TestSubjectKeysAreThosePythonIndexes(t *testing.T) {
	for _, vector := range loadGolden(t).SubjectKeys {
		t.Run(vector.Kind, func(t *testing.T) {
			subject, err := decodeSubject(vector.Subject)
			if err != nil {
				t.Fatal(err)
			}
			key, err := NewGitHubSubjectKey(7, subject)
			if err != nil {
				t.Fatal(err)
			}
			if want := (GitHubSubjectKey{RepositoryID: 7, Kind: vector.Kind, Key: vector.Key}); key != want {
				t.Errorf("key = %+v, want %+v", key, want)
			}
		})
	}
}
