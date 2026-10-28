package store

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/proto"
)

func TestDestinationKeysAreThosePythonComputes(t *testing.T) {
	for _, vector := range loadGolden(t).DestinationKeys {
		t.Run(vector.Name, func(t *testing.T) {
			if got := model.DestinationKey(vector.DestinationRef, vector.SessionID); got != vector.Key {
				t.Errorf("model.DestinationKey = %s, want %s", got, vector.Key)
			}
		})
	}
}

func TestActionEventsAreStoredAsPythonStoresThem(t *testing.T) {
	for _, vector := range loadGolden(t).ActionEvents {
		t.Run(vector.Name, func(t *testing.T) {
			var event storagecodec.ActionEvent
			if err := storagecodec.Decode(vector.Wire, &event); err != nil {
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
			var event storagecodec.ActionEvent
			if err := storagecodec.Decode(vector.Wire, &event); err == nil {
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
			want: &action_servicepb.Principal{Principal: &action_servicepb.Principal_Caller{Caller: storagecodec.ServiceAccountProto(model.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"})}},
		},
		{
			name: "operator with null account",
			json: `{"account":null,"issuer":"test-issuer","subject":"test-subject"}`,
			want: &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{Issuer: "test-issuer", Subject: "test-subject"}}},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			actor, err := storagecodec.DecodeActionActor(json.RawMessage(test.json))
			if err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(actor, test.want) {
				t.Fatalf("actor = %#v, want %#v", actor, test.want)
			}
			written, err := storagecodec.EncodeActionActor(actor)
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
