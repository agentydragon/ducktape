package store

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"reflect"
	"testing"
)

// golden.json holds what the Python service writes and accepts: the destination key of an inbox, the creation
// JSON of a subscription and the event JSON of an entry, each from the Python code that produces it, with the
// creations Python's models refuse and the pairs it tells apart. `generate_golden.py` writes it and
// `test_golden.py` checks it is current.
//
//go:embed golden.json
var goldenJSON []byte

type golden struct {
	DestinationKeys []struct {
		Name           string         `json:"name"`
		DestinationRef DestinationRef `json:"destination_ref"`
		SessionID      string         `json:"session_id"`
		Key            string         `json:"key"`
	} `json:"destination_keys"`
	// Subscriptions are valid creations: Input is what a caller sends, Stored the JSON Python writes for it.
	Subscriptions []struct {
		Name   string          `json:"name"`
		Input  json.RawMessage `json:"input"`
		Stored json.RawMessage `json:"stored"`
	} `json:"subscriptions"`
	InvalidSubscriptions []struct {
		Name  string          `json:"name"`
		Input json.RawMessage `json:"input"`
	} `json:"invalid_subscriptions"`
	// Comparisons are pairs of creations and whether Python's idempotency check calls them the same.
	Comparisons []struct {
		Name string          `json:"name"`
		A    json.RawMessage `json:"a"`
		B    json.RawMessage `json:"b"`
		Same bool            `json:"same"`
	} `json:"comparisons"`
	Events []struct {
		Name  string          `json:"name"`
		Event json.RawMessage `json:"event"`
	} `json:"events"`
}

func loadGolden(t *testing.T) golden {
	t.Helper()
	var vectors golden
	if err := decode(goldenJSON, &vectors); err != nil {
		t.Fatal(err)
	}
	return vectors
}

// sameJSON compares two JSON documents as values: key order and whitespace are not part of what a JSONB column
// keeps.
func sameJSON(t *testing.T, got, want []byte) {
	t.Helper()
	parse := func(data []byte) any {
		decoder := json.NewDecoder(bytes.NewReader(data))
		decoder.UseNumber()
		var value any
		if err := decoder.Decode(&value); err != nil {
			t.Fatalf("%s: %v", data, err)
		}
		return value
	}
	if !reflect.DeepEqual(parse(got), parse(want)) {
		t.Errorf("got  %s\nwant %s", got, want)
	}
}
