package store

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"math/big"
	"reflect"
	"testing"
)

// golden.json defines stored column forms, validation bounds and history cursors.
//
//go:embed golden.json
var goldenJSON []byte

// Valid records whether an input satisfies the store contract.
type vector struct {
	Name  string          `json:"name"`
	Input json.RawMessage `json:"input"`
	Valid bool            `json:"valid"`
}

// Stored holds the expected JSONB representation of a valid input.
type storedVector struct {
	vector
	Stored json.RawMessage `json:"stored"`
}

// storedRequest is the columns of an `action_request` row that its submission fills.
type storedRequest struct {
	Action      json.RawMessage `json:"action"`
	Arguments   json.RawMessage `json:"arguments"`
	Title       string          `json:"title"`
	Description *string         `json:"description"`
}

type golden struct {
	Enums    map[string][]string `json:"enums"`
	Requests []struct {
		vector
		Stored storedRequest `json:"stored"`
	} `json:"requests"`
	DecisionInputs   []vector `json:"decision_inputs"`
	ProviderOutcomes []struct {
		vector
		// Decisive is whether the outcome decides; `no_opinion` defers to a human.
		Decisive bool `json:"decisive"`
	} `json:"provider_outcomes"`
	PolicyEvidence []storedVector `json:"policy_evidence"`
	HistoryCursors struct {
		Encoded []struct {
			Name      string `json:"name"`
			HistoryAt string `json:"history_at"`
			ID        string `json:"id"`
			Cursor    string `json:"cursor"`
		} `json:"encoded"`
		Decoded []struct {
			Name      string `json:"name"`
			Cursor    string `json:"cursor"`
			Valid     bool   `json:"valid"`
			HistoryAt string `json:"history_at"`
			ID        string `json:"id"`
		} `json:"decoded"`
	} `json:"history_cursors"`
	TerminalStates []struct {
		ExecutionState string `json:"execution_state"`
		ActionState    string `json:"action_state"`
	} `json:"terminal_states"`
	UnknownOutcomes []struct {
		Reason string          `json:"reason"`
		Error  json.RawMessage `json:"error"`
	} `json:"unknown_outcomes"`
}

// requestInput is the submission a vector holds.
func requestInput(t *testing.T, document json.RawMessage) ActionRequestInput {
	t.Helper()
	var wire struct {
		IdempotencyKey string `json:"idempotency_key"`
		Action         struct {
			Group string `json:"group"`
			Name  string `json:"name"`
		} `json:"action"`
		Arguments   json.RawMessage `json:"arguments"`
		Title       string          `json:"title"`
		Description *string         `json:"description"`
	}
	if err := json.Unmarshal(document, &wire); err != nil {
		t.Fatal(err)
	}
	return ActionRequestInput{
		IdempotencyKey: wire.IdempotencyKey,
		Action:         ActionIdentity{Group: wire.Action.Group, Name: wire.Action.Name},
		Arguments:      wire.Arguments,
		Title:          wire.Title,
		Description:    wire.Description,
	}
}

func loadGolden(t *testing.T) golden {
	t.Helper()
	var vectors golden
	if err := json.Unmarshal(goldenJSON, &vectors); err != nil {
		t.Fatal(err)
	}
	return vectors
}

// sameJSON compares two JSON documents as values: key order, whitespace and the spelling of a number are not
// part of what a JSONB column keeps, and the numbers are compared exactly, not as floats.
func sameJSON(t *testing.T, got, want []byte) {
	t.Helper()
	parse := func(data []byte) any {
		decoder := json.NewDecoder(bytes.NewReader(data))
		decoder.UseNumber()
		var value any
		if err := decoder.Decode(&value); err != nil {
			t.Fatalf("%s: %v", data, err)
		}
		return canonical(t, value)
	}
	if !reflect.DeepEqual(parse(got), parse(want)) {
		t.Errorf("got  %s\nwant %s", got, want)
	}
}

// exactNumber is a JSON number by value.
type exactNumber string

func canonical(t *testing.T, value any) any {
	t.Helper()
	switch value := value.(type) {
	case json.Number:
		number, ok := new(big.Rat).SetString(string(value))
		if !ok {
			t.Fatalf("%s is not a number", value)
		}
		return exactNumber(number.RatString())
	case []any:
		for i, element := range value {
			value[i] = canonical(t, element)
		}
	case map[string]any:
		for key, element := range value {
			value[key] = canonical(t, element)
		}
	}
	return value
}
