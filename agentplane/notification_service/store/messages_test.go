package store

import (
	"errors"
	"strings"
	"testing"
)

func TestMessagePayloadKeepsItsStableDigestEncoding(t *testing.T) {
	fromSession := "sender-session"
	payload, err := (Message{Body: "message", FromSession: &fromSession}).payload()
	if err != nil {
		t.Fatal(err)
	}
	if got, want := string(payload), `{"body":"message","from_session":"sender-session"}`; got != want {
		t.Fatalf("message digest payload = %s, want %s", got, want)
	}

	payload, err = (Message{Body: "message"}).payload()
	if err != nil {
		t.Fatal(err)
	}
	if got, want := string(payload), `{"body":"message"}`; got != want {
		t.Fatalf("message digest payload without sender label = %s, want %s", got, want)
	}
}

func TestAMessageIsRefusedOutsideItsBounds(t *testing.T) {
	const limit = 8
	valid := Message{
		DestinationRef: DestinationRef{Namespace: "test-namespace", Name: "test-sandbox"},
		SessionID:      "test-session", IdempotencyKey: "test-key", Body: "12345678",
	}
	if err := valid.Validate(limit); err != nil {
		t.Fatalf("a body at the limit, with no UID: %v", err)
	}
	for name, change := range map[string]func(*Message){
		"empty body":          func(m *Message) { m.Body = "" },
		"body past the limit": func(m *Message) { m.Body = "1234567é" },
		"body not UTF-8":      func(m *Message) { m.Body = "\xff" },
		"empty from_session":  func(m *Message) { m.FromSession = new(string) },
		"long from_session": func(m *Message) {
			long := strings.Repeat("s", 201)
			m.FromSession = &long
		},
		"no Session":           func(m *Message) { m.SessionID = "" },
		"no idempotency key":   func(m *Message) { m.IdempotencyKey = "" },
		"no destination name":  func(m *Message) { m.DestinationRef.Name = "" },
		"long destination UID": func(m *Message) { m.DestinationRef.UID = strings.Repeat("u", 129) },
	} {
		message := valid
		change(&message)
		if err := message.Validate(limit); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: error = %v, want ErrInvalid", name, err)
		}
	}
}
