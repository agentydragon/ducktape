package threadfold

import (
	_ "embed"
	"encoding/json"
	"errors"
	"reflect"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
)

// golden.json holds scenarios folded by the Python thread fold this one replaced: the entries of each
// batch, and the rows, payload writes and evidence the Python fold produced for them, or the error it
// raised. Each batch's prior state is read back from the rows the golden earlier batches stored, so the
// row readers are held to what Python wrote as well as the builders to what it would write.
//
// The Python fold and the generator that wrote this file (`fold/generate_golden.py`) are in git history
// at commit a80ac930e0. The file is now a fixture: a deliberate change to this fold's output changes
// the expectations it makes.
//
//go:embed golden.json
var goldenJSON []byte

type golden struct {
	ThreadID        uuid.UUID        `json:"thread_id"`
	SourceID        string           `json:"source_id"`
	ProjectionEpoch string           `json:"projection_epoch"`
	Scenarios       []goldenScenario `json:"scenarios"`
}

type goldenScenario struct {
	Name    string        `json:"name"`
	Batches []goldenBatch `json:"batches"`
}

type goldenBatch struct {
	Entries     []json.RawMessage `json:"entries"`
	Settled     [][2]uint64       `json:"settled"`
	Operational *Operational      `json:"operational"`
	Extents     []struct {
		Ref   goldenRef `json:"ref"`
		Count uint64    `json:"count"`
	} `json:"extents"`
	Expected *struct {
		Rows          []json.RawMessage `json:"rows"`
		PayloadWrites []json.RawMessage `json:"payload_writes"`
		Evidence      []json.RawMessage `json:"evidence"`
	} `json:"expected"`
	Error *struct {
		Kind   string `json:"kind"`
		Cursor uint64 `json:"cursor"`
	} `json:"error"`
}

type goldenRef struct {
	ProjectionEpoch string       `json:"projection_epoch"`
	OwnerCursor     uint64       `json:"owner_cursor"`
	OwnerID         string       `json:"owner_id"`
	Field           PayloadField `json:"field"`
	RevisionCursor  uint64       `json:"revision_cursor"`
	Generation      uint64       `json:"generation"`
}

func (r goldenRef) ref() PayloadRef {
	return PayloadRef{r.ProjectionEpoch, r.OwnerCursor, r.OwnerID, r.Field, r.RevisionCursor, r.Generation}
}

type goldenWrite struct {
	Kind      string     `json:"kind"`
	Base      *goldenRef `json:"base"`
	Reference goldenRef  `json:"reference"`
	Text      string     `json:"text"`
}

// normalized round-trips a value through JSON, so numbers and maps compare the way the golden file's do.
func normalized(t *testing.T, value any) any {
	t.Helper()
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	var decoded any
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	return decoded
}

func refJSON(reference PayloadRef) map[string]any {
	return map[string]any{
		"projection_epoch": reference.ProjectionEpoch,
		"owner_cursor":     reference.OwnerCursor,
		"owner_id":         reference.OwnerID,
		"field":            reference.Field,
		"revision_cursor":  reference.RevisionCursor,
		"generation":       reference.Generation,
	}
}

func writeJSON(write PayloadWrite) map[string]any {
	if appended, ok := write.(AppendPayload); ok {
		var base any
		if appended.Base != nil {
			base = refJSON(*appended.Base)
		}
		return map[string]any{"kind": "append", "base": base, "reference": refJSON(appended.Reference), "text": appended.Text}
	}
	return map[string]any{"kind": "replace", "reference": refJSON(write.Ref()), "text": write.Fragment()}
}

func evidenceJSON(evidence EvidenceAssociation) map[string]any {
	return map[string]any{
		"projection_epoch":   evidence.ProjectionEpoch,
		"entity_cursor":      evidence.EntityCursor,
		"observation_cursor": evidence.ObservationCursor,
		"source_sequences":   append([]uint64{}, evidence.SourceSequences...),
	}
}

// goldenRows keeps the rows a scenario's golden batches stored, which the next batch's prior state is read from.
type goldenRows struct {
	sourceID string
	view     EntityRow
	items    map[string]EntityRow
	commands map[string]EntityRow
	payloads map[PayloadRef]string
}

func (g *goldenRows) viewState(t *testing.T) ViewState {
	t.Helper()
	if g.view.State == nil {
		state, err := Initial(g.sourceID, "test-epoch")
		if err != nil {
			t.Fatal(err)
		}
		return state
	}
	stored, err := ReadViewState(g.view.State)
	if err != nil {
		t.Fatal(err)
	}
	return ViewState{
		Position: Position{SourceID: g.sourceID, ProjectionEpoch: g.view.ProjectionEpoch, ThroughCursor: g.view.Cursor},
		Controls: Controls{
			AppliedModel:           stored.Controls.AppliedModel,
			ActiveTurnID:           stored.Controls.ActiveTurnID,
			HarnessState:           stored.Controls.HarnessState,
			AppliedReasoningEffort: stored.Controls.AppliedReasoningEffort,
			BlockedReason:          stored.Controls.BlockedReason,
		},
	}
}

func (g *goldenRows) prior(t *testing.T, batch EventBatch) PriorEntities {
	t.Helper()
	keys := Touched(batch)
	prior := PriorEntities{
		Items:    map[string]*Item{},
		Commands: map[string]*CommandSummary{},
		Bodies:   map[PayloadRef]string{},
	}
	for _, id := range keys.ItemIDs {
		prior.Items[id] = nil
		if row, ok := g.items[id]; ok {
			item, err := row.Item()
			if err != nil {
				t.Fatal(err)
			}
			prior.Items[id] = &item
		}
	}
	for _, id := range keys.CommandIDs {
		prior.Commands[id] = nil
		if row, ok := g.commands[id]; ok {
			command, err := row.CommandSummary()
			if err != nil {
				t.Fatal(err)
			}
			prior.Commands[id] = &command
		}
	}
	for _, completed := range keys.Completed {
		if item := prior.Items[completed.ItemID]; item != nil {
			if reference, _ := item.Payload(completed.Field); reference != nil {
				prior.Bodies[*reference] = g.payloads[*reference]
			}
		}
	}
	return prior
}

func (g *goldenRows) store(t *testing.T, batch goldenBatch) {
	t.Helper()
	for _, raw := range batch.Expected.Rows {
		var row EntityRow
		if err := json.Unmarshal(raw, &row); err != nil {
			t.Fatal(err)
		}
		switch row.EntityKind {
		case KindViewState:
			g.view = row
		case KindItem:
			g.items[row.EntityID] = row
		case KindCommand:
			g.commands[row.EntityID] = row
		}
	}
	for _, raw := range batch.Expected.PayloadWrites {
		var write goldenWrite
		if err := json.Unmarshal(raw, &write); err != nil {
			t.Fatal(err)
		}
		text := write.Text
		if write.Base != nil {
			text = g.payloads[write.Base.ref()] + text
		}
		g.payloads[write.Reference.ref()] = text
	}
}

func TestGoldenVectors(t *testing.T) {
	var vectors golden
	if err := json.Unmarshal(goldenJSON, &vectors); err != nil {
		t.Fatal(err)
	}
	if len(vectors.Scenarios) == 0 {
		t.Fatal("no golden scenarios")
	}
	for _, scenario := range vectors.Scenarios {
		t.Run(scenario.Name, func(t *testing.T) {
			stored := &goldenRows{
				sourceID: vectors.SourceID,
				items:    map[string]EntityRow{},
				commands: map[string]EntityRow{},
				payloads: map[PayloadRef]string{},
			}
			for number, golden := range scenario.Batches {
				batch := EventBatch{SourceID: vectors.SourceID}
				state := stored.viewState(t)
				batch.AfterCursor = state.Position.ThroughCursor
				for _, raw := range golden.Entries {
					entry := &eventlogpb.EventEntry{}
					if err := protojson.Unmarshal(raw, entry); err != nil {
						t.Fatal(err)
					}
					batch.Entries = append(batch.Entries, entry)
				}
				for _, settled := range golden.Settled {
					batch.Settled = append(batch.Settled, &historypb.CursorRange{First: settled[0], Last: settled[1]})
				}
				result, err := Advance(state, batch, stored.prior(t, batch))
				if golden.Error != nil {
					var notUnderstood *ObservationNotUnderstoodError
					var contract *ContractError
					switch {
					case golden.Error.Kind == "not_understood" && errors.As(err, &notUnderstood):
						if notUnderstood.Cursor != golden.Error.Cursor {
							t.Errorf("batch %d: cursor = %d, want %d", number, notUnderstood.Cursor, golden.Error.Cursor)
						}
					case golden.Error.Kind == "contract" && errors.As(err, &contract):
						if contract.Cursor != golden.Error.Cursor {
							t.Errorf("batch %d: cursor = %d, want %d", number, contract.Cursor, golden.Error.Cursor)
						}
					default:
						t.Errorf("batch %d: error = %v, want a %s error", number, err, golden.Error.Kind)
					}
					return
				}
				if err != nil {
					t.Fatalf("batch %d: %v", number, err)
				}
				extents := map[PayloadRef]uint64{}
				for _, extent := range golden.Extents {
					extents[extent.Ref.ref()] = extent.Count
				}
				rows, err := OrderedEntityRows(vectors.ThreadID, result, extents, golden.Operational)
				if err != nil {
					t.Fatalf("batch %d: %v", number, err)
				}
				writes, evidence := []any{}, []any{}
				for _, write := range result.PayloadWrites {
					writes = append(writes, writeJSON(write))
				}
				for _, association := range result.EvidenceUpserts {
					evidence = append(evidence, evidenceJSON(association))
				}
				want := golden.Expected
				for name, pair := range map[string][2]any{
					"rows":          {normalized(t, rows), normalized(t, want.Rows)},
					"payload write": {normalized(t, writes), normalized(t, want.PayloadWrites)},
					"evidence":      {normalized(t, evidence), normalized(t, want.Evidence)},
				} {
					if !reflect.DeepEqual(pair[0], pair[1]) {
						t.Errorf("batch %d: %s differ:\n got %v\nwant %v", number, name, pair[0], pair[1])
					}
				}
				stored.store(t, golden)
			}
		})
	}
}
