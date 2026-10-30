package centralsampler

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// Expectations come from Codex rust-v0.162.1 core/src/tools/code_mode/mod.rs tests
// truncated_text_output_starts_with_warning and over_budget_audio_output_is_omitted.
func TestTruncateCodeModeResultMatchesCodex(t *testing.T) {
	text, err := truncateCodeModeResult([]ContentPart{{Type: "input_text", Text: "0123456789012345678901234567890123456789"}}, 5)
	if err != nil {
		t.Fatal(err)
	}
	expected := "Warning: truncated output (original token count: 10)\nTotal output lines: 1\n\n0123456789…5 tokens truncated…0123456789"
	if len(text) != 1 || text[0].Text != expected {
		t.Fatalf("truncated text = %+v", text)
	}
	var audio ContentPart
	if err := json.Unmarshal([]byte(`{"type":"input_audio","audio_url":"data:audio/wav;base64,`+strings.Repeat("A", 100)+`"}`), &audio); err != nil {
		t.Fatal(err)
	}
	omitted, err := truncateCodeModeResult([]ContentPart{audio}, 5)
	if err != nil {
		t.Fatal(err)
	}
	if len(omitted) != 1 || omitted[0].Text != "[omitted 1 audio items ...]" {
		t.Fatalf("over-budget audio = %+v", omitted)
	}
}

func TestTruncateCodeModeResultKeepsImagesAndBudgetsText(t *testing.T) {
	var image ContentPart
	if err := json.Unmarshal([]byte(`{"type":"input_image","image_url":"data:image/png;base64,opaque"}`), &image); err != nil {
		t.Fatal(err)
	}
	items, err := truncateCodeModeResult([]ContentPart{
		{Type: "input_text", Text: strings.Repeat("a", 40)}, image, {Type: "input_text", Text: "later"},
	}, 5)
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 3 || items[1].Type != "input_image" || items[2].Text != "[omitted 1 text items ...]" {
		t.Fatalf("mixed truncation = %+v", items)
	}
	if !strings.Contains(items[0].Text, "tokens truncated") {
		t.Fatalf("first text item was not middle-truncated: %q", items[0].Text)
	}
}

func TestCellOutputPutsStatusHeaderFirst(t *testing.T) {
	failure := "boom"
	for _, tc := range []struct {
		kind   codeObservationKind
		error  *string
		status string
		last   string
	}{
		{codeYielded, nil, "Script running with cell ID session:7", "partial"},
		{codeTerminated, nil, "Script terminated", "partial"},
		{codeResult, nil, "Script completed", "partial"},
		{codeResult, &failure, "Script failed", "Script error:\nboom"},
	} {
		observation := codeObservation{Kind: tc.kind, Cell: codeCellOutput{
			CellID: "7", ContentItems: []json.RawMessage{json.RawMessage(`{"type":"input_text","text":"partial"}`)}, ErrorText: tc.error,
		}}
		items, err := cellOutput(observation, "session:7", nil, 1250*time.Millisecond)
		if err != nil {
			t.Fatal(err)
		}
		if items[0].Text != tc.status+"\nWall time 1.3 seconds\nOutput:\n" || items[len(items)-1].Text != tc.last {
			t.Fatalf("%s output = %+v", tc.kind, items)
		}
	}
}

// Cases from Codex rust-v0.162.1 utils/string/src/truncate/tests.rs.
func TestTruncateMiddleWithTokenBudgetMatchesUpstream(t *testing.T) {
	for _, tc := range []struct {
		input     string
		maxTokens int
		want      string
	}{
		{"short output", 100, "short output"},
		{"😀😀😀😀😀😀😀😀😀😀\nsecond line with text\n", 8, "😀😀😀😀…8 tokens truncated… line with text\n"},
		{"a😀b😀c", 1, "a…2 tokens truncated…c"},
		{"é中😀é", 1, "é…2 tokens truncated…é"},
		{"😀😀😀", 1, "…2 tokens truncated…"},
	} {
		if got := truncateMiddleWithTokenBudget(tc.input, tc.maxTokens); got != tc.want {
			t.Errorf("truncateMiddleWithTokenBudget(%q, %d)=%q, want %q", tc.input, tc.maxTokens, got, tc.want)
		}
	}
}
