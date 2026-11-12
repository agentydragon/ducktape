package storage

import (
	"bytes"
	"testing"
)

func TestContentIDsIgnoreObjectOrderAndPreserveNumbers(t *testing.T) {
	first, err := CanonicalContent(ContentKind("fixture/item"), []byte(`{"type":"message","extra":900719925474099312345,"nested":{"b":2,"a":1}}`))
	if err != nil {
		t.Fatal(err)
	}
	reordered, err := CanonicalContent(ContentKind("fixture/item"), []byte(`{ "nested": {"a":1,"b":2}, "extra":900719925474099312345,"type":"message"}`))
	if err != nil {
		t.Fatal(err)
	}
	if first.ID != reordered.ID || !bytes.Equal(first.Payload, reordered.Payload) {
		t.Fatal("JSON object order changed content identity")
	}
	if !bytes.Contains(first.Payload, []byte("900719925474099312345")) {
		t.Fatal("content hashing rounded provider number")
	}
	otherKind, err := CanonicalContent(ContentKind("fixture/part"), first.Payload)
	if err != nil {
		t.Fatal(err)
	}
	if first.ID == otherKind.ID {
		t.Fatal("content kind was omitted from content identity")
	}
	first.Payload = []byte(`{"type":"changed"}`)
	if VerifyContent(first) == nil {
		t.Fatal("changed stored payload passed hash verification")
	}
}

func TestContentIDsRejectInvalidKindsAndTrailingJSON(t *testing.T) {
	if _, err := CanonicalContent(ContentKind(""), []byte(`{}`)); err == nil {
		t.Fatal("empty content kind accepted")
	}
	for _, raw := range []string{`{}`, `{} {}`, `{} garbage`} {
		_, err := CanonicalContent(ContentKind("fixture/item"), []byte(raw))
		if (err == nil) != (raw == `{}`) {
			t.Fatalf("content %q error=%v", raw, err)
		}
	}
}
