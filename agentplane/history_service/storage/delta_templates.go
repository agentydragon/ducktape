package storage

import (
	"encoding/json"
	"errors"
	"io"
	"strconv"
	"strings"
)

// The exact shapes of the streamed delta frames whose removal loses only how a response was chunked.
//
// A frame matches only when its parsed JSON has exactly a template's keys at every level and every
// value has the template's type. Each field is a literal, an identity the caller compares with the
// item's other frames, the chunk's content, a group key, or per-chunk data the template drops (the
// frame id, a timestamp, a token estimate). Anything else, including a key a newer harness adds, is
// not a match. The shapes were taken from staging frames on 2026-10-10 (Claude Code 2.1.x, Codex
// app-server); a template is added, never edited, so a stored template name keeps its meaning.
type deltaTemplate string

const (
	templateClaudeText            deltaTemplate = "claude.stream_event.text_delta@1"
	templateClaudeThinking        deltaTemplate = "claude.stream_event.thinking_delta@1"
	templateClaudeInputJSON       deltaTemplate = "claude.stream_event.input_json_delta@1"
	templateCodexAgentMessage     deltaTemplate = "codex.item/agentMessage/delta@1"
	templateCodexReasoningSummary deltaTemplate = "codex.item/reasoning/summaryTextDelta@1"
	templateCodexCommandOutput    deltaTemplate = "codex.item/commandExecution/outputDelta@1"
)

// delta is a matched frame: a claudeDelta or a codexDelta.
type delta interface {
	templateName() deltaTemplate
	text() string
}

type claudeDelta struct {
	template        deltaTemplate
	sessionID       string
	parentToolUseID *string
	index           int64
	content         string
}

func (d claudeDelta) templateName() deltaTemplate { return d.template }
func (d claudeDelta) text() string                { return d.content }

type codexDelta struct {
	template                 deltaTemplate
	threadID, turnID, itemID string
	content                  string
	// summaryIndex is the Codex reasoning summary part; nil for templates without one.
	summaryIndex *int64
}

func (d codexDelta) templateName() deltaTemplate { return d.template }
func (d codexDelta) text() string                { return d.content }

var errDuplicateKey = errors.New("duplicate JSON object key")

// parseStrict returns the frame's JSON object, or false for anything a lossy parse could
// misrepresent: a duplicate key, trailing data, or a value that is not an object. Numbers are
// json.Number so no digit is lost.
func parseStrict(line string) (map[string]any, bool) {
	decoder := json.NewDecoder(strings.NewReader(line))
	decoder.UseNumber()
	value, err := decodeStrict(decoder)
	if err != nil {
		return nil, false
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return nil, false
	}
	object, ok := value.(map[string]any)
	return object, ok
}

func decodeStrict(decoder *json.Decoder) (any, error) {
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	opening, isDelimiter := token.(json.Delim)
	if !isDelimiter {
		return token, nil
	}
	if opening == '[' {
		array := []any{}
		for decoder.More() {
			element, err := decodeStrict(decoder)
			if err != nil {
				return nil, err
			}
			array = append(array, element)
		}
		_, err := decoder.Token()
		return array, err
	}
	object := map[string]any{}
	for decoder.More() {
		key, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		if _, repeated := object[key.(string)]; repeated {
			return nil, errDuplicateKey
		}
		if object[key.(string)], err = decodeStrict(decoder); err != nil {
			return nil, err
		}
	}
	_, err = decoder.Token()
	return object, err
}

// integer reads a JSON integer literal; a float, a boolean or an out-of-range value is not one.
func integer(value any) (int64, bool) {
	number, ok := value.(json.Number)
	if !ok || strings.ContainsAny(string(number), ".eE") {
		return 0, false
	}
	parsed, err := strconv.ParseInt(string(number), 10, 64)
	return parsed, err == nil
}

// hasKeys reports whether the object has exactly the given keys.
func hasKeys(object map[string]any, keys ...string) bool {
	if len(object) != len(keys) {
		return false
	}
	for _, key := range keys {
		if _, ok := object[key]; !ok {
			return false
		}
	}
	return true
}

var claudeDeltas = map[string]struct {
	template   deltaTemplate
	contentKey string
	keySets    [][]string
}{
	"text_delta": {templateClaudeText, "text", [][]string{{"type", "text"}}},
	"thinking_delta": {
		templateClaudeThinking, "thinking",
		[][]string{{"type", "thinking"}, {"type", "thinking", "estimated_tokens"}},
	},
	"input_json_delta": {templateClaudeInputJSON, "partial_json", [][]string{{"type", "partial_json"}}},
}

var codexDeltas = map[string]struct {
	template deltaTemplate
	keys     []string
}{
	"item/agentMessage/delta": {templateCodexAgentMessage, []string{"threadId", "turnId", "itemId", "delta"}},
	"item/reasoning/summaryTextDelta": {
		templateCodexReasoningSummary, []string{"threadId", "turnId", "itemId", "delta", "summaryIndex"},
	},
	"item/commandExecution/outputDelta": {templateCodexCommandOutput, []string{"threadId", "turnId", "itemId", "delta"}},
}

// matchDelta returns the frame as a delta, or nil when it matches no template exactly.
func matchDelta(frame map[string]any) delta {
	switch {
	case hasKeys(frame, "type", "event", "session_id", "parent_tool_use_id", "uuid"):
		if matched, ok := matchClaude(frame); ok {
			return matched
		}
	case hasKeys(frame, "method", "params", "emittedAtMs"):
		if matched, ok := matchCodex(frame); ok {
			return matched
		}
	}
	return nil
}

func matchClaude(frame map[string]any) (claudeDelta, bool) {
	event, _ := frame["event"].(map[string]any)
	sessionID, sessionIDIsString := frame["session_id"].(string)
	_, uuidIsString := frame["uuid"].(string)
	var parent *string
	switch value := frame["parent_tool_use_id"].(type) {
	case nil:
	case string:
		parent = &value
	default:
		return claudeDelta{}, false
	}
	if frame["type"] != "stream_event" || !uuidIsString || !sessionIDIsString ||
		!hasKeys(event, "type", "index", "delta") || event["type"] != "content_block_delta" {
		return claudeDelta{}, false
	}
	index, indexIsInteger := integer(event["index"])
	body, bodyIsObject := event["delta"].(map[string]any)
	if !indexIsInteger || !bodyIsObject {
		return claudeDelta{}, false
	}
	kind, _ := body["type"].(string)
	known, ok := claudeDeltas[kind]
	if !ok {
		return claudeDelta{}, false
	}
	content, contentIsString := body[known.contentKey].(string)
	matchesKeys := false
	for _, keys := range known.keySets {
		matchesKeys = matchesKeys || hasKeys(body, keys...)
	}
	if estimate, present := body["estimated_tokens"]; present {
		if _, isInteger := integer(estimate); !isInteger {
			return claudeDelta{}, false
		}
	}
	if !matchesKeys || !contentIsString {
		return claudeDelta{}, false
	}
	return claudeDelta{known.template, sessionID, parent, index, content}, true
}

func matchCodex(frame map[string]any) (codexDelta, bool) {
	method, methodIsString := frame["method"].(string)
	params, paramsIsObject := frame["params"].(map[string]any)
	if _, emittedAtIsInteger := integer(frame["emittedAtMs"]); !methodIsString || !emittedAtIsInteger || !paramsIsObject {
		return codexDelta{}, false
	}
	known, ok := codexDeltas[method]
	if !ok || !hasKeys(params, known.keys...) {
		return codexDelta{}, false
	}
	threadID, threadIDIsString := params["threadId"].(string)
	turnID, turnIDIsString := params["turnId"].(string)
	itemID, itemIDIsString := params["itemId"].(string)
	content, contentIsString := params["delta"].(string)
	if !threadIDIsString || !turnIDIsString || !itemIDIsString || !contentIsString {
		return codexDelta{}, false
	}
	var summaryIndex *int64
	if value := params["summaryIndex"]; value != nil {
		index, isInteger := integer(value)
		if !isInteger || index < 0 {
			return codexDelta{}, false
		}
		summaryIndex = &index
	}
	return codexDelta{known.template, threadID, turnID, itemID, content, summaryIndex}, true
}
