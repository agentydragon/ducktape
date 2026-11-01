package claude

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/anthropics/anthropic-sdk-go"
)

// The SDK's permissive response decoder must not turn malformed native evidence
// into successful, empty transcript observations.
func TestClaudeBlocksRejectMalformedKnownVariants(t *testing.T) {
	for _, raw := range []string{
		`[{"type":"text","text":42}]`,
		`[{"type":"text"}]`,
		`[{"type":"thinking","thinking":[]}]`,
		`[{"type":"tool_use","id":3,"name":"Bash","input":{}}]`,
		`[{"type":"tool_use","id":"call","name":"Bash"}]`,
		`[{"type":42}]`,
		`[null]`,
	} {
		t.Run(raw, func(t *testing.T) {
			if _, err := claudeBlocks(json.RawMessage(raw)); err == nil {
				t.Fatal("malformed block accepted")
			}
		})
	}
}

func TestClaudeBlocksRetainUnknownVariantBetweenKnownBlocks(t *testing.T) {
	raw := `[{"type":"text","text":"before"},{"type":"future_block","payload":{"large":9007199254740993,"empty":null}},{"type":"thinking","thinking":"after"}]`
	blocks, err := claudeBlocks(json.RawMessage(raw))
	if err != nil {
		t.Fatal(err)
	}
	if len(blocks) != 3 {
		t.Fatalf("block indexes changed: %d", len(blocks))
	}
	if text, ok := blocks[0].AsAny().(anthropic.TextBlock); !ok || text.Text != "before" {
		t.Fatalf("text variant lost: %#v", blocks[0])
	}
	if blocks[1].AsAny() != nil || blocks[1].RawJSON() != `{"type":"future_block","payload":{"large":9007199254740993,"empty":null}}` {
		t.Fatalf("unknown variant changed: %s", blocks[1].RawJSON())
	}
	if thinking, ok := blocks[2].AsAny().(anthropic.ThinkingBlock); !ok || thinking.Thinking != "after" {
		t.Fatalf("thinking variant lost: %#v", blocks[2])
	}
}

func TestClaudeStreamRejectsMalformedKnownVariants(t *testing.T) {
	for _, raw := range []string{
		`{"type":"message_start","message":{"id":42}}`,
		`{"type":"content_block_start","index":"0","content_block":{"type":"text","text":""}}`,
		`{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call","name":42,"input":{}}}`,
		`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":42}}`,
		`{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":false}}`,
		`{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta"}}`,
	} {
		t.Run(raw, func(t *testing.T) {
			var frame claudeWireFrame
			if err := json.Unmarshal([]byte(`{"type":"stream_event","event":`+raw+`}`), &frame); err != nil {
				t.Fatal(err)
			}
			adapter := &claudeAdapter{}
			if err := adapter.handleStreamEvent(context.Background(), frame, 1, []uint64{1}); err == nil {
				t.Fatal("malformed stream event accepted")
			}
		})
	}
}
