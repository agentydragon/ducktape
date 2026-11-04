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
	"google.golang.org/protobuf/proto"
)

// golden.json holds scenarios folded by the Python thread fold this one replaced: the entries of each
// batch, and the protobuf rows, payload writes and evidence the Python fold produced for them, or the error it
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
	Operational json.RawMessage   `json:"operational"`
	Extents     []struct {
		Ref   json.RawMessage `json:"ref"`
		Count uint64          `json:"count"`
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

type goldenWrite struct {
	Kind      string           `json:"kind"`
	Base      *json.RawMessage `json:"base"`
	Reference json.RawMessage  `json:"reference"`
	Text      string           `json:"text"`
}

func goldenPayloadRef(t *testing.T, data json.RawMessage) PayloadRef {
	t.Helper()
	var reference historypb.ThreadPayloadReference
	if err := protojson.Unmarshal(data, &reference); err != nil {
		t.Fatal(err)
	}
	return PayloadRef{
		ProjectionEpoch: reference.ProjectionEpoch,
		OwnerCursor:     reference.OwnerCursor,
		OwnerID:         reference.OwnerId,
		Field:           reference.Field,
		RevisionCursor:  reference.RevisionCursor,
		Generation:      reference.Generation,
	}
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

func refJSON(t *testing.T, reference PayloadRef) json.RawMessage {
	t.Helper()
	encoded, err := protojson.Marshal(&historypb.ThreadPayloadReference{
		ProjectionEpoch: reference.ProjectionEpoch,
		OwnerCursor:     reference.OwnerCursor,
		OwnerId:         reference.OwnerID,
		Field:           reference.Field,
		RevisionCursor:  reference.RevisionCursor,
		Generation:      reference.Generation,
	})
	if err != nil {
		t.Fatal(err)
	}
	return encoded
}

func writeJSON(t *testing.T, write PayloadWrite) map[string]any {
	t.Helper()
	if appended, ok := write.(AppendPayload); ok {
		var base any
		if appended.Base != nil {
			base = refJSON(t, *appended.Base)
		}
		return map[string]any{"kind": "append", "base": base, "reference": refJSON(t, appended.Reference), "text": appended.Text}
	}
	return map[string]any{"kind": "replace", "reference": refJSON(t, write.Ref()), "text": write.Fragment()}
}

func evidenceJSON(evidence EvidenceAssociation) map[string]any {
	return map[string]any{
		"projection_epoch":   evidence.ProjectionEpoch,
		"entity_cursor":      evidence.EntityCursor,
		"observation_cursor": evidence.ObservationCursor,
		"source_sequences":   append([]uint64{}, evidence.SourceSequences...),
	}
}

// Golden rows use the same protobuf representation as the runtime fold.
func goldenEntity(t *testing.T, raw json.RawMessage) *historypb.ThreadEntity {
	t.Helper()
	entity := &historypb.ThreadEntity{}
	if err := protojson.Unmarshal(raw, entity); err != nil {
		t.Fatal(err)
	}
	return entity
}

// goldenRows keeps the rows a scenario's golden batches stored, which the next batch's prior state is read from.
type goldenRows struct {
	sourceID string
	view     *historypb.ThreadEntity
	items    map[string]*historypb.ThreadEntity
	commands map[string]*historypb.ThreadEntity
	payloads map[PayloadRef]string
}

func (g *goldenRows) viewState(t *testing.T) ViewState {
	t.Helper()
	if g.view == nil {
		state, err := Initial(g.sourceID, "test-epoch")
		if err != nil {
			t.Fatal(err)
		}
		return state
	}
	stored := g.view.GetViewState()
	if stored == nil || stored.Controls == nil {
		t.Fatal("missing view state")
	}
	return ViewState{
		Position: Position{SourceID: g.sourceID, ProjectionEpoch: g.view.ProjectionEpoch, ThroughCursor: g.view.Cursor},
		Controls: Controls{
			AppliedModel:           stored.Controls.AppliedModel,
			ActiveTurnID:           stored.Controls.ActiveTurnId,
			HarnessState:           stored.Controls.HarnessState,
			AppliedReasoningEffort: stored.Controls.AppliedReasoningEffort,
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
			item, err := ItemFromEntity(row)
			if err != nil {
				t.Fatal(err)
			}
			prior.Items[id] = &item
		}
	}
	for _, id := range keys.CommandIDs {
		prior.Commands[id] = nil
		if row, ok := g.commands[id]; ok {
			command, err := CommandFromEntity(row)
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
		row := goldenEntity(t, raw)
		switch row.Entity.(type) {
		case *historypb.ThreadEntity_ViewState:
			g.view = row
		case *historypb.ThreadEntity_Item:
			g.items[row.EntityId] = row
		case *historypb.ThreadEntity_Command:
			g.commands[row.EntityId] = row
		}
	}
	for _, raw := range batch.Expected.PayloadWrites {
		var write goldenWrite
		if err := json.Unmarshal(raw, &write); err != nil {
			t.Fatal(err)
		}
		text := write.Text
		if write.Base != nil {
			text = g.payloads[goldenPayloadRef(t, *write.Base)] + text
		}
		g.payloads[goldenPayloadRef(t, write.Reference)] = text
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
				items:    map[string]*historypb.ThreadEntity{},
				commands: map[string]*historypb.ThreadEntity{},
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
					extents[goldenPayloadRef(t, extent.Ref)] = extent.Count
				}
				var operational *historypb.ThreadOperational
				if len(golden.Operational) != 0 && string(golden.Operational) != "null" {
					operational = &historypb.ThreadOperational{}
					if err := protojson.Unmarshal(golden.Operational, operational); err != nil {
						t.Fatal(err)
					}
				}
				rows, err := OrderedEntityRows(vectors.ThreadID, result, extents, operational)
				if err != nil {
					t.Fatalf("batch %d: %v", number, err)
				}
				writes, evidence := []any{}, []any{}
				for _, write := range result.PayloadWrites {
					writes = append(writes, writeJSON(t, write))
				}
				for _, association := range result.EvidenceUpserts {
					evidence = append(evidence, evidenceJSON(association))
				}
				want := golden.Expected
				if len(rows) != len(want.Rows) {
					t.Errorf("batch %d: got %d rows, want %d", number, len(rows), len(want.Rows))
				} else {
					for i, raw := range want.Rows {
						expected := goldenEntity(t, raw)
						if !proto.Equal(rows[i], expected) {
							t.Errorf("batch %d row %d: got %v want %v", number, i, rows[i], expected)
						}
					}
				}
				for name, pair := range map[string][2]any{
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
