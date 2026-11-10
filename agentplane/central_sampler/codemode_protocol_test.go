package centralsampler

import (
	"encoding/json"
	"testing"
)

func TestCodeModeRejectsAmbiguousOrUnknownRuntimeOutcome(t *testing.T) {
	// These externally tagged variants belong to the pinned host protocol, not our journal.
	for _, raw := range []string{
		`{"Result":{"cell_id":"c","content_items":[]},"Yielded":{"cell_id":"c","content_items":[]}}`,
		`{"FutureResult":{"cell_id":"c","content_items":[]}}`,
		`{"Result":{"content_items":[]}}`,
		`{"Result":{"cell_id":"c"}}`,
	} {
		var observation codeObservation
		if err := decodeProtocolObject([]byte(raw), &observation); err == nil {
			t.Fatalf("accepted malformed host observation %s", raw)
		}
	}
	for _, raw := range []string{
		`{"type":"wait/completed","outcome":{"FutureOutcome":{"Result":{"cell_id":"c","content_items":[]}}}}`,
		`{"type":"session/ready","outcome":{"LiveCell":{"Result":{"cell_id":"c","content_items":[]}}}}`,
		`{"type":"wait/completed","outcome":{"LiveCell":{"Result":{"cell_id":"c","content_items":[]}},"MissingCell":{"Result":{"cell_id":"c","content_items":[]}}}}`,
	} {
		if _, err := decodeWaitObservation(json.RawMessage(raw)); err == nil {
			t.Fatalf("accepted malformed wait response %s", raw)
		}
	}
}

func TestCodeModeDecodesPinnedWaitRuntimeVariants(t *testing.T) {
	// WireWaitOutcome/WireRuntimeResponse in upstream code-mode-protocol/src/host/payload.rs.
	observation, err := decodeWaitObservation(json.RawMessage(`{"type":"wait/completed","outcome":{"LiveCell":{"Yielded":{"cell_id":"cell-1","content_items":[{"type":"input_text","text":"partial"}],"code_mode_host_duration_ns":123}}}}`))
	if err != nil {
		t.Fatal(err)
	}
	if observation.Kind != codeYielded || observation.Cell.CellID != "cell-1" || len(observation.Cell.ContentItems) != 1 {
		t.Fatalf("wrong runtime observation: %+v", observation)
	}
}
