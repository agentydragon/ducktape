package protocol

import (
	"encoding/json"
	"testing"
)

func TestFunctionCallOutputBodyUnmarshal(t *testing.T) {
	var output FunctionCallOutputBody
	if err := json.Unmarshal([]byte(`"done"`), &output); err != nil {
		t.Fatal(err)
	}
	if output.String == nil || *output.String != "done" {
		t.Fatalf("decoded string = %v, want %q", output.String, "done")
	}

	var items FunctionCallOutputBody
	if err := json.Unmarshal([]byte(`[{"type":"input_text","text":"done"}]`), &items); err != nil {
		t.Fatal(err)
	}
	if got := items.FunctionCallOutputContentItemArray; len(got) != 1 {
		t.Fatalf("decoded item count = %d, want 1", len(got))
	}
	item := items.FunctionCallOutputContentItemArray[0]
	if item.Type != FunctionCallOutputContentItemTypeInputText || item.Text == nil || *item.Text != "done" {
		t.Fatalf("decoded item = %+v, want input_text containing %q", item, "done")
	}

	var invalid FunctionCallOutputBody
	if err := json.Unmarshal([]byte(`{"unexpected":true}`), &invalid); err == nil {
		t.Fatal("object decoded as a function call output body")
	}
}

func TestNetworkAccessUnionUnmarshal(t *testing.T) {
	var disabled NetworkAccessUnion
	if err := json.Unmarshal([]byte("false"), &disabled); err != nil {
		t.Fatal(err)
	}
	if disabled.Bool == nil || *disabled.Bool {
		t.Fatalf("decoded bool = %v, want false", disabled.Bool)
	}

	var enabled NetworkAccessUnion
	if err := json.Unmarshal([]byte(`"enabled"`), &enabled); err != nil {
		t.Fatal(err)
	}
	if enabled.Enum == nil || *enabled.Enum != Enabled {
		t.Fatalf("decoded enum = %v, want %q", enabled.Enum, Enabled)
	}

	var invalid NetworkAccessUnion
	if err := json.Unmarshal([]byte(`{"unexpected":true}`), &invalid); err == nil {
		t.Fatal("object decoded as a network access union")
	}
}
