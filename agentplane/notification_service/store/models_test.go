package store

import (
	"encoding/json"
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
