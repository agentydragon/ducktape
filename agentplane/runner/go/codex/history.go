package codex

import (
	"bufio"
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/runner/go/harness"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

type codexHistoryRecord struct {
	Type    string          `json:"type"`
	Payload json.RawMessage `json:"payload"`
}

type codexPayload struct {
	Type    string `json:"type"`
	Role    string `json:"role"`
	ID      string `json:"id"`
	CallID  string `json:"call_id"`
	Output  string `json:"output"`
	Text    string `json:"text"`
	Content []struct {
		Text string `json:"text"`
	} `json:"content"`
	Summary []struct {
		Text string `json:"text"`
	} `json:"summary"`
}

// readCodexHistory extracts continuation items from Codex's model-facing rollout, which differs
// from the app-server thread projection. A nil map means the transcript cannot establish a view.
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
		var payload codexPayload
		if err := json.Unmarshal(record.Payload, &payload); err != nil {
			return nil, err
		}
		switch payload.Type {
		case "message":
			if payload.Role != "assistant" {
				continue
			}
			var text strings.Builder
			for _, part := range payload.Content {
				text.WriteString(part.Text)
			}
			texts[payload.ID] = text.String()
		case "reasoning":
			var summary strings.Builder
			for i, part := range payload.Summary {
				if i > 0 {
					summary.WriteByte('\n')
				}
				summary.WriteString(part.Text)
			}
			summaries[payload.ID] = summary.String()
		case "function_call":
			calls[payload.CallID] = true
		case "function_call_output":
			outputs[payload.CallID] = payload.Output
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
