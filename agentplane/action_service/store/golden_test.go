package store

import (
	"encoding/json"
	"errors"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
)

func TestVocabulariesAreThoseOfTheStoredColumns(t *testing.T) {
	enums := loadGolden(t).Enums
	for column, want := range map[string][]string{
		"action_state":    storedStateNames(),
		"execution_state": toStrings(executionStates),
		"verdict":         {string(VerdictAllow), string(VerdictDeny)},
		"policy_kind":     toStrings(policyKinds),
		"policy_source":   {PolicySourceRules},
	} {
		if got := enums[column]; !slices.Equal(got, want) {
			t.Errorf("%s: stored vocabulary has %v, the store %v", column, got, want)
		}
	}
}

func TestUnknownOutcomeReasonsBelongToStoredVocabulary(t *testing.T) {
	stored := loadGolden(t).Enums["unknown_outcome_reason"]
	for _, reason := range []UnknownOutcomeReason{UnknownExecutorLost, UnknownLeaseExpired, UnknownAdapterOutcome, UnknownCoordinatorStopped} {
		if !slices.Contains(stored, string(reason)) {
			t.Errorf("stored vocabulary has no reason %s among %v", reason, stored)
		}
	}
}

func TestTerminalStatesMapToStoredRequestStates(t *testing.T) {
	vectors := loadGolden(t).TerminalStates
	got := map[ExecutionState]action_servicepb.ActionState{}
	for _, vector := range vectors {
		got[ExecutionState(vector.ExecutionState)] = mustStateFromStored(t, vector.ActionState)
	}
	if !reflect.DeepEqual(got, terminalActionStates) || len(vectors) != len(terminalActionStates) {
		t.Errorf("stored mapping is %v, the store %v", got, terminalActionStates)
	}
}

func TestEndedExecutionsHoldStoredErrors(t *testing.T) {
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

func TestRequestsSatisfyStoredContract(t *testing.T) {
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

func TestDecisionInputsSatisfyValidationContract(t *testing.T) {
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
				t.Errorf("valid input = %v, Validate = %v", vector.Valid, err)
			}
		})
	}
}

func TestVotesRequireDecisiveOutcomes(t *testing.T) {
	for _, vector := range loadGolden(t).ProviderOutcomes {
		t.Run(vector.Name, func(t *testing.T) {
			var wire struct {
				Verdict           Verdict         `json:"verdict"`
				ReasonCode        string          `json:"reason_code"`
				ReasonDescription *string         `json:"reason_description"`
				Evidence          *PolicyEvidence `json:"evidence"`
			}
			// A vote must decide; `no_opinion` defers to a human.
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

func TestPolicyEvidenceRoundTripsStoredForms(t *testing.T) {
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

func TestHistoryCursorsSatisfyStoredContract(t *testing.T) {
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

func mustStateFromStored(t *testing.T, stored string) action_servicepb.ActionState {
	t.Helper()
	state, known := stateFromStored(stored)
	if !known {
		t.Fatalf("unknown stored Action state %q", stored)
	}
	return state
}
