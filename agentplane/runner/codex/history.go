package codex

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/runner/harness"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

type codexHistoryRecord struct {
	Type    string          `json:"type"`
	Payload json.RawMessage `json:"payload"`
}

type codexResponseItemType struct {
	Type string `json:"type"`
}

// Codex CLI v0.152.0 serializes both tool-output response items with
// FunctionCallOutputBody: a string or an array of FunctionCallOutputContentItem.
// This history fold projects arrays to text and currently supports InputText
// items only. The upstream definitions are pinned here:
// ResponseItem: https://github.com/openai/codex/blob/316795b3cf2a45e90d121d9f46499d4658b2645c/codex-rs/protocol/src/models.rs#L1072-L1129
// Content items: https://github.com/openai/codex/blob/316795b3cf2a45e90d121d9f46499d4658b2645c/codex-rs/protocol/src/models.rs#L2036-L2053
// Output body: https://github.com/openai/codex/blob/316795b3cf2a45e90d121d9f46499d4658b2645c/codex-rs/protocol/src/models.rs#L2116-L2131
type codexInputText struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

type codexContentPart struct {
	Text string `json:"text"`
}

type codexSummaryPart struct {
	Text string `json:"text"`
}

type codexMessagePayload struct {
	ID      string             `json:"id"`
	Role    string             `json:"role"`
	Content []codexContentPart `json:"content"`
}

type codexReasoningPayload struct {
	ID      string             `json:"id"`
	Summary []codexSummaryPart `json:"summary"`
}

type codexFunctionCallPayload struct {
	CallID string `json:"call_id"`
}

type codexFunctionCallOutputPayload struct {
	CallID string          `json:"call_id"`
	Output codexToolOutput `json:"output"`
}

type codexCustomToolCallPayload struct {
	CallID string `json:"call_id"`
}

type codexCustomToolCallOutputPayload struct {
	CallID string          `json:"call_id"`
	Output codexToolOutput `json:"output"`
}

type codexToolOutputValue interface {
	text() string
}

type codexToolOutputText string

func (output codexToolOutputText) text() string { return string(output) }

type codexToolOutputTextParts []codexInputText

func (parts codexToolOutputTextParts) text() string {
	var text strings.Builder
	for _, part := range parts {
		text.WriteString(part.Text)
	}
	return text.String()
}

type codexToolOutput struct {
	value codexToolOutputValue
}

func (output *codexToolOutput) UnmarshalJSON(data []byte) error {
	var text string
	if err := json.Unmarshal(data, &text); err == nil {
		output.value = codexToolOutputText(text)
		return nil
	}
	var parts []codexInputText
	if err := json.Unmarshal(data, &parts); err != nil {
		return fmt.Errorf("expected a string or array of input_text: %w", err)
	}
	for _, part := range parts {
		if part.Type != "input_text" {
			return fmt.Errorf("unsupported content type %q", part.Type)
		}
	}
	output.value = codexToolOutputTextParts(parts)
	return nil
}

func (output codexToolOutput) text() string {
	if output.value == nil {
		return ""
	}
	return output.value.text()
}

// readCodexHistory extracts continuation items from Codex's model-facing rollout, which differs
// from the app-server thread projection. In pinned Codex 0.157.0, legacy API history omits a
// completed shell call and merges reasoning IDs that survive in the next model request. New
// paginated history preserves those tested items; this is not proof that it needs private parsing.
// See agentplane/debug/codex_app_server_history_apis.md and the native test_history_projection test.
// A nil map means the transcript cannot establish a view.
func readCodexHistory(directory, sessionID string, observed map[string]harness.ObservedItem) (map[string]harness.ObservedItem, error) {
	paths := make([]string, 0, 1)
	sessionsDir := filepath.Join(directory, "sessions")
	err := filepath.WalkDir(sessionsDir, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if !entry.IsDir() && strings.HasSuffix(entry.Name(), sessionID+".jsonl") {
			paths = append(paths, path)
		}
		return nil
	})
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if len(paths) != 1 {
		return nil, nil
	}
	file, err := os.Open(paths[0])
	if err != nil {
		return nil, err
	}
	defer file.Close()

	texts := make(map[string]string)
	summaries := make(map[string]string)
	calls := make(map[string]bool)
	outputs := make(map[string]string)
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), harness.MaxFrameLineBytes)
	for scanner.Scan() {
		var record codexHistoryRecord
		if err := json.Unmarshal(scanner.Bytes(), &record); err != nil {
			return nil, err
		}
		if record.Type == "compacted" {
			return nil, nil
		}
		if record.Type == "event_msg" {
			var payload struct {
				Type string `json:"type"`
			}
			if err := json.Unmarshal(record.Payload, &payload); err != nil {
				return nil, err
			}
			if payload.Type == "thread_rolled_back" {
				return nil, nil
			}
		}
		if record.Type != "response_item" {
			continue
		}
		var payload codexResponseItemType
		if err := json.Unmarshal(record.Payload, &payload); err != nil {
			return nil, err
		}
		switch payload.Type {
		case "message":
			var message codexMessagePayload
			if err := json.Unmarshal(record.Payload, &message); err != nil {
				return nil, fmt.Errorf("decode message response item: %w", err)
			}
			if message.Role != "assistant" {
				continue
			}
			var text strings.Builder
			for _, part := range message.Content {
				text.WriteString(part.Text)
			}
			texts[message.ID] = text.String()
		case "reasoning":
			var reasoning codexReasoningPayload
			if err := json.Unmarshal(record.Payload, &reasoning); err != nil {
				return nil, fmt.Errorf("decode reasoning response item: %w", err)
			}
			var summary strings.Builder
			for i, part := range reasoning.Summary {
				if i > 0 {
					summary.WriteByte('\n')
				}
				summary.WriteString(part.Text)
			}
			summaries[reasoning.ID] = summary.String()
		case "function_call":
			var call codexFunctionCallPayload
			if err := json.Unmarshal(record.Payload, &call); err != nil {
				return nil, fmt.Errorf("decode function call response item: %w", err)
			}
			calls[call.CallID] = true
		case "function_call_output":
			var output codexFunctionCallOutputPayload
			if err := json.Unmarshal(record.Payload, &output); err != nil {
				return nil, fmt.Errorf("decode function call output response item: %w", err)
			}
			outputs[output.CallID] = output.Output.text()
		case "custom_tool_call":
			var call codexCustomToolCallPayload
			if err := json.Unmarshal(record.Payload, &call); err != nil {
				return nil, fmt.Errorf("decode custom tool call response item: %w", err)
			}
			calls[call.CallID] = true
		case "custom_tool_call_output":
			var output codexCustomToolCallOutputPayload
			if err := json.Unmarshal(record.Payload, &output); err != nil {
				return nil, fmt.Errorf("decode custom tool call output response item: %w", err)
			}
			outputs[output.CallID] = output.Output.text()
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}

	recovered := make(map[string]harness.ObservedItem)
	for itemID, item := range observed {
		switch item.Kind {
		case eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT:
			if text, ok := texts[itemID]; ok {
				item.Text = text
				recovered[itemID] = item
			}
		case eventpb.ItemKind_ITEM_KIND_TOOL_CALL:
			if calls[itemID] {
				output, ok := outputs[itemID]
				if !ok {
					output = "aborted"
				}
				// Shell results wrap the observed stdout in execution metadata.
				if item.Completed && strings.Contains(output, item.Output) {
					output = item.Output
				}
				item.Output = output
				recovered[itemID] = item
			}
		case eventpb.ItemKind_ITEM_KIND_REASONING:
			if text, ok := summaries[itemID]; ok {
				item.Text = text
				recovered[itemID] = item
			}
		default:
			if item.Completed {
				recovered[itemID] = item
			}
		}
	}
	return recovered, nil
}
