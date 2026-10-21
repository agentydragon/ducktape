package store

import (
	"encoding/json"
	"errors"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestVocabulariesAreThoseOfTheStoredColumns(t *testing.T) {
	enums := loadGolden(t).Enums
	for column, want := range map[string][]string{
		"action_state":    toStrings(actionStates),
		"execution_state": toStrings(executionStates),
		"verdict":         {string(VerdictAllow), string(VerdictDeny)},
		"policy_kind":     toStrings(policyKinds),
		"policy_source":   {PolicySourceRules},
	} {
		if got := enums[column]; !slices.Equal(got, want) {
			t.Errorf("%s: Python has %v, the store %v", column, got, want)
		}
	}
}

func TestTheUnknownOutcomeReasonsAreAmongThoseOfPython(t *testing.T) {
	python := loadGolden(t).Enums["unknown_outcome_reason"]
	for _, reason := range []UnknownOutcomeReason{UnknownExecutorLost, UnknownLeaseExpired, UnknownAdapterOutcome, UnknownCoordinatorStopped} {
		if !slices.Contains(python, string(reason)) {
			t.Errorf("Python has no reason %s among %v", reason, python)
		}
	}
}

func TestTerminalStatesGiveTheirRequestsThePythonState(t *testing.T) {
	vectors := loadGolden(t).TerminalStates
	got := map[ExecutionState]ActionState{}
	for _, vector := range vectors {
		got[ExecutionState(vector.ExecutionState)] = ActionState(vector.ActionState)
	}
	if !reflect.DeepEqual(got, terminalActionStates) || len(vectors) != len(terminalActionStates) {
		t.Errorf("Python maps %v, the store %v", got, terminalActionStates)
	}
}

func TestEndedExecutionsHoldTheErrorsPythonWrites(t *testing.T) {
	for _, vector := range loadGolden(t).UnknownOutcomes {
		t.Run(vector.Reason, func(t *testing.T) {
			sameJSON(t, []byte(unknownOutcomeError(UnknownOutcomeReason(vector.Reason))), vector.Error)
		})
	}
}

func toStrings[T ~string](values []T) []string {
	strings := make([]string, len(values))
	for i, value := range values {
		strings[i] = string(value)
	}
	return strings
}

func TestRequestsAreValidatedAndStoredAsPythonDoes(t *testing.T) {
	for _, vector := range loadGolden(t).Requests {
		t.Run(vector.Name, func(t *testing.T) {
			input := requestInput(t, vector.Input)
			err := input.Validate()
			if !vector.Valid {
				if !errors.Is(err, ErrInvalid) {
					t.Errorf("Validate = %v, want ErrInvalid", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			action, err := json.Marshal(input.Action)
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, action, vector.Stored.Action)
			sameJSON(t, input.Arguments, vector.Stored.Arguments)
			if input.Title != vector.Stored.Title || !reflect.DeepEqual(input.Description, vector.Stored.Description) {
				t.Errorf("title and description %q, %v are not stored as %q, %v", input.Title, input.Description,
					vector.Stored.Title, vector.Stored.Description)
			}
			var read ActionIdentity
			if err := json.Unmarshal(vector.Stored.Action, &read); err != nil || read != input.Action {
				t.Errorf("the stored action reads back as %+v, %v", read, err)
			}
		})
	}
}

func TestDecisionInputsAreValidatedAsPythonDoes(t *testing.T) {
	for _, vector := range loadGolden(t).DecisionInputs {
		t.Run(vector.Name, func(t *testing.T) {
			var wire struct {
				Verdict         Verdict `json:"verdict"`
				ExpectedVersion int64   `json:"expected_version"`
				IdempotencyKey  string  `json:"idempotency_key"`
				DecisionNote    *string `json:"decision_note"`
			}
			if err := json.Unmarshal(vector.Input, &wire); err != nil {
				t.Fatal(err)
			}
			err := DecisionInput(wire).Validate()
			if vector.Valid != (err == nil) || err != nil && !errors.Is(err, ErrInvalid) {
				t.Errorf("Python accepts it = %v, Validate = %v", vector.Valid, err)
			}
		})
	}
}

func TestVotesAreValidatedAsPythonValidatesOutcomes(t *testing.T) {
	for _, vector := range loadGolden(t).ProviderOutcomes {
		t.Run(vector.Name, func(t *testing.T) {
			var wire struct {
				Verdict           Verdict         `json:"verdict"`
				ReasonCode        string          `json:"reason_code"`
				ReasonDescription *string         `json:"reason_description"`
				Evidence          *PolicyEvidence `json:"evidence"`
			}
			// A vote is an outcome that decides: Python's ProviderVerdict adds `no_opinion`, which defers to a human.
			err := json.Unmarshal(vector.Input, &wire)
			if err == nil {
				err = ProviderVote{
					Provider: "test-provider", Verdict: wire.Verdict, ReasonCode: wire.ReasonCode,
					ReasonDescription: wire.ReasonDescription, Evidence: wire.Evidence,
				}.Validate()
			}
			if want := vector.Valid && vector.Decisive; want != (err == nil) {
				t.Errorf("a vote that decides = %v, Validate = %v", want, err)
			}
		})
	}
}

func TestPolicyEvidenceIsReadAndWrittenAsPythonDoes(t *testing.T) {
	for _, vector := range loadGolden(t).PolicyEvidence {
		t.Run(vector.Name, func(t *testing.T) {
			var sent, stored PolicyEvidence
			err := json.Unmarshal(vector.Input, &sent)
			if !vector.Valid {
				if err == nil {
					t.Errorf("accepted %s", vector.Input)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			written, err := json.Marshal(sent)
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, written, vector.Stored)
			if err := json.Unmarshal(vector.Stored, &stored); err != nil || !reflect.DeepEqual(stored, sent) {
				t.Errorf("the stored form reads back as %+v, %v; the input was %+v", stored, err, sent)
			}
		})
	}
}

func TestActorsAreReadAndWrittenAsPythonDoes(t *testing.T) {
	for _, vector := range loadGolden(t).Actors {
		t.Run(vector.Name, func(t *testing.T) {
			actor, err := decodeActor(vector.Input)
			if !vector.Valid {
				if err == nil {
					t.Errorf("accepted %s", vector.Input)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if _, caller := actor.(CallerPrincipal); caller != (vector.Kind == "caller") {
				t.Errorf("read a %T, want a %s", actor, vector.Kind)
			}
			written, err := json.Marshal(actor)
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, written, vector.Stored)
		})
	}
}

func TestHistoryCursorsAreThoseOfPython(t *testing.T) {
	cursors := loadGolden(t).HistoryCursors
	for _, vector := range cursors.Encoded {
		t.Run("encode "+vector.Name, func(t *testing.T) {
			at, err := time.Parse(time.RFC3339Nano, vector.HistoryAt)
			if err != nil {
				t.Fatal(err)
			}
			if got := encodeCursor(historyPosition{At: at, ID: uuid.MustParse(vector.ID)}); got != vector.Cursor {
				t.Errorf("encodeCursor = %s, want %s", got, vector.Cursor)
			}
		})
	}
	for _, vector := range cursors.Decoded {
		t.Run("decode "+vector.Name, func(t *testing.T) {
			position, err := decodeCursor(vector.Cursor)
			if !vector.Valid {
				if !errors.Is(err, ErrInvalid) {
					t.Errorf("decodeCursor = %+v, %v; want ErrInvalid", position, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			at, err := time.Parse(time.RFC3339Nano, vector.HistoryAt)
			if err != nil {
				t.Fatal(err)
			}
			if !position.At.Equal(at) || position.ID != uuid.MustParse(vector.ID) {
				t.Errorf("decodeCursor = %+v, want %s %s", position, vector.HistoryAt, vector.ID)
			}
		})
	}
}
