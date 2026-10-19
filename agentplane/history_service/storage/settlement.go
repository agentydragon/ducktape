package storage

import (
	"encoding/json"
	"maps"
	"slices"
	"strconv"
	"strings"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	historypb "github.com/agentydragon/ducktape/agentplane/proto/history_service"
)

// Settlement decides whether an item field's streamed deltas can leave stored history.
//
// settle is a pure function of one item's span of runner entries, ending at the entry that completes
// the field. It settles only when every streamed delta of that field is a pair, a native frame
// matching an exact template and the runner's derived delta made from it, and the retained native
// completion frame holds exactly their concatenation. Any doubt keeps every entry: what a settlement
// drops (chunking, per-chunk timestamps and ids, frame serialization) is all it drops.

type field int

const (
	fieldText field = iota
	fieldArguments
	fieldOutput
)

type target struct {
	itemID string
	field  field
}

// anomalies are observations that may signal a harness or runner problem. A span holding one keeps
// every packet, so the frames stay available to turn into a harness/runner test.
var anomalies = map[string]bool{
	"command_failed":          true,
	"harness_stderr":          true,
	"harness_lost":            true,
	"harness_exited":          true,
	"harness_launch_failed":   true,
	"conversation_reconciled": true,
	"turn_completed":          true,
}

var fieldTemplates = map[field][]deltaTemplate{
	fieldText: {
		templateClaudeText, templateClaudeThinking, templateCodexAgentMessage, templateCodexReasoningSummary,
	},
	fieldArguments: {templateClaudeInputJSON},
	fieldOutput:    {templateCodexCommandOutput},
}

// completionTarget is the item field whose content the entry completes, or nil.
func completionTarget(entry *eventlogpb.EventEntry) *target {
	switch observation := entry.GetEvent().GetObservation().(type) {
	case *eventpb.Event_ItemCompleted:
		completed := observation.ItemCompleted
		if _, isText := completed.GetOutcome().(*eventpb.ItemCompleted_Text); isText {
			return &target{completed.GetItemId(), fieldText}
		}
		return &target{completed.GetItemId(), fieldOutput}
	case *eventpb.Event_ToolArguments:
		return &target{observation.ToolArguments.GetItemId(), fieldArguments}
	}
	return nil
}

func startedItem(entry *eventlogpb.EventEntry) (string, bool) {
	started := entry.GetEvent().GetItemStarted()
	return started.GetItemId(), started != nil
}

// deltaContent is the content of the target's derived delta Event; false for any other entry.
func deltaContent(entry *eventlogpb.EventEntry, item target) (string, bool) {
	switch observation := entry.GetEvent().GetObservation().(type) {
	case *eventpb.Event_TextDelta:
		if item.field == fieldText && observation.TextDelta.GetItemId() == item.itemID {
			return observation.TextDelta.GetText(), true
		}
	case *eventpb.Event_ToolArgumentsDelta:
		if item.field == fieldArguments && observation.ToolArgumentsDelta.GetItemId() == item.itemID {
			return observation.ToolArgumentsDelta.GetPartialJson(), true
		}
	case *eventpb.Event_ToolOutputDelta:
		if item.field == fieldOutput && observation.ToolOutputDelta.GetItemId() == item.itemID {
			return observation.ToolOutputDelta.GetText(), true
		}
	}
	return "", false
}

// nativeFrame is the parsed frame of a native entry from the harness, or nil.
func nativeFrame(entry *eventlogpb.EventEntry) map[string]any {
	native := entry.GetEvent().GetNative()
	if native == nil || native.GetDirection() != eventpb.Direction_DIRECTION_FROM_HARNESS {
		return nil
	}
	frame, _ := parseStrict(native.GetLine())
	return frame
}

func oneSource(entry *eventlogpb.EventEntry) (uint64, bool) {
	sources := entry.GetEvent().GetSourceSequences()
	if len(sources) != 1 {
		return 0, false
	}
	return sources[0], true
}

type pair struct {
	entry   *eventlogpb.EventEntry
	source  uint64
	matched delta
}

// settle takes span in cursor order from the item's ItemStarted (and its native source) through the
// completing entry, which is last. Stored gaps from earlier settlements are allowed. It returns nil
// when the span does not settle.
func settle(span []*eventlogpb.EventEntry) *historypb.SettledDeltas {
	if len(span) == 0 {
		return nil
	}
	completion := span[len(span)-1]
	item := completionTarget(completion)
	if item == nil {
		return nil
	}
	byCursor := make(map[uint64]*eventlogpb.EventEntry, len(span))
	for _, entry := range span {
		byCursor[entry.GetCursor()] = entry
	}
	for _, entry := range span {
		native := entry.GetEvent().GetNative()
		if anomalies[observationKind(entry)] ||
			(native != nil && native.GetDirection() == eventpb.Direction_DIRECTION_FROM_HARNESS && nativeFrame(entry) == nil) {
			return nil
		}
	}
	sourceFrame := func(entry *eventlogpb.EventEntry) map[string]any {
		if source, ok := oneSource(entry); ok {
			return nativeFrame(byCursor[source])
		}
		return nil
	}
	completionFrame := sourceFrame(completion)
	if completionFrame == nil {
		return nil
	}
	var starts []*eventlogpb.EventEntry
	for _, entry := range span {
		if started, ok := startedItem(entry); ok && started == item.itemID {
			starts = append(starts, entry)
		}
	}
	if len(starts) != 1 {
		return nil
	}
	startFrame := sourceFrame(starts[0])

	var pairs []pair
	for _, entry := range span {
		content, isDelta := deltaContent(entry, *item)
		if !isDelta {
			continue
		}
		source, hasSource := oneSource(entry)
		var matched delta
		if frame := sourceFrame(entry); frame != nil {
			matched = matchDelta(frame)
		}
		if !hasSource || matched == nil || matched.text() != content ||
			!slices.Contains(fieldTemplates[item.field], matched.templateName()) {
			return nil
		}
		pairs = append(pairs, pair{entry, source, matched})
	}
	if len(pairs) == 0 {
		return nil
	}
	template := pairs[0].matched.templateName()
	elided := map[uint64]bool{}
	deltas := make([]delta, len(pairs))
	for i, p := range pairs {
		if p.matched.templateName() != template {
			return nil
		}
		elided[p.entry.GetCursor()], elided[p.source] = true, true
		deltas[i] = p.matched
	}
	// A removed native frame must be evidence for its own delta only.
	for _, entry := range span {
		if elided[entry.GetCursor()] {
			continue
		}
		for _, source := range entry.GetEvent().GetSourceSequences() {
			if elided[source] {
				return nil
			}
		}
	}
	if !identitiesAgree(deltas, *item, completionFrame, startFrame) || !completionHolds(deltas, *item, completion, completionFrame) {
		return nil
	}
	settlement := &historypb.SettledDeltas{
		ItemId:           item.itemID,
		Template:         string(template),
		CompletionCursor: completion.GetCursor(),
		ChunkCount:       uint64(len(pairs)),
		FirstChunkAt:     pairs[0].entry.GetEvent().GetAt(),
		LastChunkAt:      pairs[len(pairs)-1].entry.GetEvent().GetAt(),
	}
	for _, cursorRange := range cursorRanges(elided) {
		settlement.Ranges = append(settlement.Ranges, &historypb.CursorRange{First: cursorRange.First, Last: cursorRange.Last})
	}
	return settlement
}

// cursorRanges merges the cursors into ascending ranges of adjacent ones.
func cursorRanges(cursors map[uint64]bool) []CursorRange {
	var ranges []CursorRange
	for _, cursor := range slices.Sorted(maps.Keys(cursors)) {
		if last := len(ranges) - 1; last >= 0 && ranges[last].Last == cursor-1 {
			ranges[last].Last = cursor
		} else {
			ranges = append(ranges, CursorRange{cursor, cursor})
		}
	}
	return ranges
}

func identitiesAgree(deltas []delta, item target, completion, start map[string]any) bool {
	if _, claude := deltas[0].(claudeDelta); claude {
		// Claude names the block by its index in the streamed message, which the item's
		// content_block_start established.
		event, _ := start["event"].(map[string]any)
		if start["type"] != "stream_event" || event["type"] != "content_block_start" {
			return false
		}
		for _, d := range deltas {
			c, isClaude := d.(claudeDelta)
			if !isClaude || !sameString(c.sessionID, completion["session_id"]) ||
				!sameOptionalString(c.parentToolUseID, completion["parent_tool_use_id"]) || !sameInteger(c.index, event["index"]) {
				return false
			}
		}
		return true
	}
	params, isObject := completion["params"].(map[string]any)
	if !isObject {
		return false
	}
	for _, d := range deltas {
		c, isCodex := d.(codexDelta)
		if !isCodex || c.itemID != item.itemID || !sameString(c.threadID, params["threadId"]) || !sameString(c.turnID, params["turnId"]) {
			return false
		}
	}
	return true
}

func sameString(want string, got any) bool {
	text, ok := got.(string)
	return ok && text == want
}

func sameOptionalString(want *string, got any) bool {
	if want == nil {
		return got == nil
	}
	return sameString(*want, got)
}

func sameInteger(want int64, got any) bool {
	value, ok := integer(got)
	return ok && value == want
}

// completionHolds reports whether the retained completion frame, and the runner's completion Event,
// hold exactly the concatenation.
func completionHolds(deltas []delta, item target, completion *eventlogpb.EventEntry, frame map[string]any) bool {
	var builder strings.Builder
	for _, d := range deltas {
		builder.WriteString(d.text())
	}
	text := builder.String()
	switch template := deltas[0].templateName(); template {
	case templateClaudeText, templateClaudeThinking:
		key := "text"
		if template == templateClaudeThinking {
			key = "thinking"
		}
		return completion.GetEvent().GetItemCompleted().GetText() == text &&
			slices.ContainsFunc(claudeBlocks(frame, item.itemID), func(block map[string]any) bool { return sameString(text, block[key]) })
	case templateClaudeInputJSON:
		parsed, ok := parseStrict(text)
		if !ok {
			return false
		}
		if arguments, ok := parseStrict(completion.GetEvent().GetToolArguments().GetArgumentsJson()); !ok || !jsonEqual(arguments, parsed) {
			return false
		}
		return slices.ContainsFunc(claudeBlocks(frame, item.itemID), func(block map[string]any) bool {
			return block["type"] == "tool_use" && block["id"] == item.itemID && jsonEqual(block["input"], parsed)
		})
	case templateCodexAgentMessage:
		message := codexItem(frame, item.itemID, "agentMessage")
		return message != nil && sameString(text, message["text"]) && text == completion.GetEvent().GetItemCompleted().GetText()
	case templateCodexReasoningSummary:
		reasoning := codexItem(frame, item.itemID, "reasoning")
		summary, isArray := reasoning["summary"].([]any)
		if !isArray {
			return false
		}
		parts := map[int64]*strings.Builder{}
		for _, d := range deltas {
			c, isCodex := d.(codexDelta)
			if !isCodex || c.summaryIndex == nil {
				return false
			}
			if parts[*c.summaryIndex] == nil {
				parts[*c.summaryIndex] = &strings.Builder{}
			}
			parts[*c.summaryIndex].WriteString(c.content)
		}
		for index, part := range parts {
			if index >= int64(len(summary)) || !sameString(part.String(), summary[index]) {
				return false
			}
		}
		return true
	case templateCodexCommandOutput:
		command := codexItem(frame, item.itemID, "commandExecution")
		return command != nil && sameString(text, command["aggregatedOutput"]) &&
			text == completion.GetEvent().GetItemCompleted().GetTool().GetOutput()
	}
	return false
}

// claudeBlocks are the content blocks of the retained `assistant` frame for this item's message.
func claudeBlocks(frame map[string]any, itemID string) []map[string]any {
	message, isObject := frame["message"].(map[string]any)
	if frame["type"] != "assistant" || !isObject {
		return nil
	}
	// Text and thinking items are `<message id>#<block index>`; tool calls are the tool_use id.
	if before, _, found := strings.Cut(itemID, "#"); found && message["id"] != before {
		return nil
	}
	content, _ := message["content"].([]any)
	var blocks []map[string]any
	for _, element := range content {
		if block, isObject := element.(map[string]any); isObject {
			blocks = append(blocks, block)
		}
	}
	return blocks
}

func codexItem(frame map[string]any, itemID, itemType string) map[string]any {
	params, _ := frame["params"].(map[string]any)
	item, _ := params["item"].(map[string]any)
	if frame["method"] != "item/completed" || item["id"] != itemID || item["type"] != itemType {
		return nil
	}
	return item
}

// jsonEqual compares decoded JSON values, numbers by value. A boolean never equals a number.
func jsonEqual(left, right any) bool {
	switch left := left.(type) {
	case map[string]any:
		right, ok := right.(map[string]any)
		if !ok || len(left) != len(right) {
			return false
		}
		for key, value := range left {
			other, present := right[key]
			if !present || !jsonEqual(value, other) {
				return false
			}
		}
		return true
	case []any:
		right, ok := right.([]any)
		return ok && slices.EqualFunc(left, right, jsonEqual)
	case json.Number:
		right, ok := right.(json.Number)
		return ok && numbersEqual(left, right)
	default:
		return left == right
	}
}

func isIntegerLiteral(number json.Number) bool { return !strings.ContainsAny(string(number), ".eE") }

func numbersEqual(left, right json.Number) bool {
	if left == right {
		return true
	}
	if isIntegerLiteral(left) && isIntegerLiteral(right) {
		return strings.TrimPrefix(string(left), "-") == "0" && strings.TrimPrefix(string(right), "-") == "0"
	}
	leftFloat, leftErr := strconv.ParseFloat(string(left), 64)
	rightFloat, rightErr := strconv.ParseFloat(string(right), 64)
	return leftErr == nil && rightErr == nil && leftFloat == rightFloat
}
