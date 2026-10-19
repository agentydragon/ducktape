package storage

import (
	"encoding/json"
	"reflect"
	"testing"
)

// The frame shapes are copied from staging Claude Code 2.1.x and Codex app-server deltas (2026-10-10).

func parsed(t *testing.T, frame map[string]any) map[string]any {
	t.Helper()
	line, err := json.Marshal(frame)
	if err != nil {
		t.Fatal(err)
	}
	object, ok := parseStrict(string(line))
	if !ok {
		t.Fatalf("%s does not parse", line)
	}
	return object
}

func claudeFrame(delta map[string]any, overrides map[string]any) map[string]any {
	frame := map[string]any{
		"type":               "stream_event",
		"event":              map[string]any{"type": "content_block_delta", "index": 2, "delta": delta},
		"session_id":         "test-session",
		"parent_tool_use_id": nil,
		"uuid":               "test-uuid",
	}
	for key, value := range overrides {
		frame[key] = value
	}
	return frame
}

func codexFrame(method string, extra map[string]any) map[string]any {
	params := map[string]any{"threadId": "test-thread", "turnId": "test-turn", "itemId": "test-item", "delta": "chunk"}
	for key, value := range extra {
		params[key] = value
	}
	return map[string]any{"method": method, "params": params, "emittedAtMs": 1760000000000}
}

func TestKnownShapesMatch(t *testing.T) {
	parent, one := "toolu_parent", int64(1)
	for name, test := range map[string]struct {
		frame map[string]any
		want  delta
	}{
		"claude text": {
			claudeFrame(map[string]any{"type": "text_delta", "text": "chunk"}, nil),
			claudeDelta{templateClaudeText, "test-session", nil, 2, "chunk"},
		},
		"claude thinking": {
			claudeFrame(map[string]any{"type": "thinking_delta", "thinking": "chunk", "estimated_tokens": 3}, nil),
			claudeDelta{templateClaudeThinking, "test-session", nil, 2, "chunk"},
		},
		"claude input json": {
			claudeFrame(map[string]any{"type": "input_json_delta", "partial_json": `{"a"`}, map[string]any{"parent_tool_use_id": "toolu_parent"}),
			claudeDelta{templateClaudeInputJSON, "test-session", &parent, 2, `{"a"`},
		},
		"codex agent message": {
			codexFrame("item/agentMessage/delta", nil),
			codexDelta{templateCodexAgentMessage, "test-thread", "test-turn", "test-item", "chunk", nil},
		},
		"codex reasoning summary": {
			codexFrame("item/reasoning/summaryTextDelta", map[string]any{"summaryIndex": 1}),
			codexDelta{templateCodexReasoningSummary, "test-thread", "test-turn", "test-item", "chunk", &one},
		},
		"codex command output": {
			codexFrame("item/commandExecution/outputDelta", nil),
			codexDelta{templateCodexCommandOutput, "test-thread", "test-turn", "test-item", "chunk", nil},
		},
	} {
		if got := matchDelta(parsed(t, test.frame)); !reflect.DeepEqual(got, test.want) {
			t.Errorf("%s: got %v, want %v", name, got, test.want)
		}
	}
}

func TestOtherShapesDoNotMatch(t *testing.T) {
	text := map[string]any{"type": "text_delta", "text": "chunk"}
	for name, frame := range map[string]map[string]any{
		"unknown frame key":     claudeFrame(text, map[string]any{"extra": "a key a newer harness adds"}),
		"unknown delta key":     claudeFrame(map[string]any{"type": "text_delta", "text": "chunk", "citations": []any{}}, nil),
		"boolean token count":   claudeFrame(map[string]any{"type": "thinking_delta", "thinking": "chunk", "estimated_tokens": true}, nil),
		"signature delta":       claudeFrame(map[string]any{"type": "signature_delta", "signature": "sig"}, nil),
		"non-string text":       claudeFrame(map[string]any{"type": "text_delta", "text": 7}, nil),
		"boolean index":         claudeFrame(text, map[string]any{"event": map[string]any{"type": "content_block_delta", "index": true, "delta": text}}),
		"float index":           claudeFrame(text, map[string]any{"event": map[string]any{"type": "content_block_delta", "index": 2.5, "delta": text}}),
		"summary index on text": codexFrame("item/agentMessage/delta", map[string]any{"summaryIndex": 0}),
		"negative summary":      codexFrame("item/reasoning/summaryTextDelta", map[string]any{"summaryIndex": -1}),
		"unknown method":        codexFrame("item/plan/delta", nil),
		"string timestamp":      {"method": "item/agentMessage/delta", "params": codexFrame("x", nil)["params"], "emittedAtMs": "1760000000000"},
	} {
		if got := matchDelta(parsed(t, frame)); got != nil {
			t.Errorf("%s matched %v", name, got)
		}
	}
	reasoning := codexFrame("item/reasoning/summaryTextDelta", nil)
	if got := matchDelta(parsed(t, reasoning)); got != nil {
		t.Errorf("a reasoning delta without its summary index matched %v", got)
	}
}

func TestParseStrictRefusesLossyFrames(t *testing.T) {
	for _, line := range []string{`{"a": 1, "a": 2}`, `{"a": {"b": 1, "b": 1}}`, `{"a": [{"b": 1, "b": 1}]}`, `{"a": 1} {"b": 2}`, `[1]`, `not json`, ``} {
		if object, ok := parseStrict(line); ok {
			t.Errorf("%q parsed as %v", line, object)
		}
	}
}
