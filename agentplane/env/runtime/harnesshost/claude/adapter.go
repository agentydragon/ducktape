package claude

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/harness"
	"github.com/anthropics/anthropic-sdk-go"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
	"github.com/schlunsen/claude-agent-sdk-go/types"
)

const (
	claudeSessionName = "agentplane"
	claudeMaxRetries  = 2
)

var claudeTools = []string{"Bash", "Edit", "Read", "Agent", "SendMessage", "TaskOutput"}

type claudePendingInput struct {
	commandID string
	text      string
}

type claudeStartedInput struct {
	messageUUID string
	sequence    uint64
}

type claudeToolResult struct {
	messageID string
	sequence  uint64
}

type claudeChange struct {
	commandID string
	value     string
}

type claudeAdapter struct {
	host   harness.AdapterHost
	launch harness.LaunchConfig

	mu              sync.Mutex
	nativeSessionID string
	pending         map[string]claudePendingInput
	started         []claudeStartedInput
	toolResult      *claudeToolResult
	modelChanges    map[string]claudeChange
	effortChanges   map[string]claudeChange
	messageID       string
	blockItems      map[int64]string
	adapterID       string
}

// New creates an adapter bound to one runner-owned Session.
func New(host harness.AdapterHost, launch harness.LaunchConfig) harness.ProcessAdapter {
	snapshot := host.Session()
	nativeID := snapshot.NativeSessionID
	if nativeID == "" {
		nativeID = uuid.NewString()
	}
	return &claudeAdapter{
		host: host, launch: launch, nativeSessionID: nativeID,
		pending:      make(map[string]claudePendingInput),
		modelChanges: make(map[string]claudeChange), effortChanges: make(map[string]claudeChange),
		blockItems: make(map[int64]string), adapterID: uuid.NewString(),
	}
}

func (a *claudeAdapter) Command() ([]string, error) {
	session := a.host.Session()
	args := []string{
		a.launch.Binary,
		"--safe-mode",
		"--prompt-suggestions=false",
		"--setting-sources=",
		"--strict-mcp-config",
		"--name", claudeSessionName,
		"--tools", strings.Join(claudeTools, ","),
		"--output-format", "stream-json",
		"--verbose",
		"--permission-mode", "bypassPermissions",
		"--permission-prompt-tool", "stdio",
		"--include-partial-messages",
		"--input-format", "stream-json",
		"--model", session.Model,
		"--disable-slash-commands",
	}
	if session.Resuming {
		args = append(args, "--resume", a.nativeSessionID)
	} else {
		args = append(args, "--session-id", a.nativeSessionID)
	}
	args = append(args, "--replay-user-messages")
	if session.ReasoningEffort != "" {
		args = append(args, "--effort", session.ReasoningEffort)
	}
	return args, nil
}

func (a *claudeAdapter) Environment() (map[string]string, error) {
	session := a.host.Session()
	configDir := filepath.Join(session.Directory, "claude")
	if err := os.MkdirAll(configDir, 0o700); err != nil {
		return nil, err
	}
	environment := make(map[string]string)
	environment["ANTHROPIC_AUTH_TOKEN"] = a.launch.Token
	environment["ANTHROPIC_BASE_URL"] = a.launch.BaseURL
	environment["CLAUDE_CONFIG_DIR"] = configDir
	environment["CLAUDE_CODE_MAX_RETRIES"] = fmt.Sprint(claudeMaxRetries)
	// TODO: Consider exporting native Claude OpenTelemetry to an Agentplane collector.
	environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
	if session.TotalContextBudgetTokens != nil && *session.TotalContextBudgetTokens != 0 {
		environment["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = fmt.Sprint(*session.TotalContextBudgetTokens)
	}
	return environment, nil
}

func (a *claudeAdapter) BlocksCommandDispatch() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.modelChanges) != 0 || len(a.effortChanges) != 0
}

func (a *claudeAdapter) Handshake(ctx context.Context) (string, error) {
	requestID := nativeRequestID()
	frame, err := marshalClaudeControlRequest(requestID, claudeInitializeRequest{
		SDKControlInitializeRequest: types.SDKControlInitializeRequest{Subtype: "initialize"},
		AppendSystemPrompt:          a.host.Session().Instructions,
	})
	if err != nil {
		return "", err
	}
	receipt, err := a.host.RequestNativeHandshake(ctx, frame, func(raw json.RawMessage) bool {
		parsed, err := decodeClaudeControlResponse(raw)
		return err == nil && parsed.RequestID == requestID
	})
	if err != nil {
		return "", fmt.Errorf("wait for Claude initialization: %w", err)
	}
	response, err := decodeClaudeControlResponse(receipt.Frame)
	if err != nil {
		return "", err
	}
	if response.Subtype == "error" {
		return "", fmt.Errorf("Claude initialization rejected: %s", *response.Error)
	}

	a.mu.Lock()
	nativeID := a.nativeSessionID
	a.mu.Unlock()
	return nativeID, nil
}

func (a *claudeAdapter) Recover(ctx context.Context) error {
	requests, err := a.host.Journal().PendingNativeRequests(ctx)
	if err != nil {
		return err
	}
	correlations := map[string]bool{}
	var replayFrom uint64
	a.mu.Lock()
	for _, request := range requests {
		if len(request.Commands) != 1 {
			a.mu.Unlock()
			return fmt.Errorf("Claude request %q must carry one command", request.ID)
		}
		command := request.Commands[0]
		if command.TerminalCursor != nil {
			continue
		}
		id := command.Command.GetCommandId()
		switch request.Kind {
		case harness.ClaudeSubmit:
			input := command.Command.GetSubmitInput()
			if input == nil || input.Text == "" {
				a.mu.Unlock()
				return fmt.Errorf("Claude submit request %q requires input", request.ID)
			}
			a.pending[request.ID] = claudePendingInput{commandID: id, text: input.Text}
		case harness.ClaudeModelChange:
			model := command.Command.GetChangeModel()
			if model == nil || model.Model == "" {
				a.mu.Unlock()
				return fmt.Errorf("Claude model request %q requires a model command", request.ID)
			}
			a.modelChanges[request.ID] = claudeChange{commandID: id, value: model.Model}
		case harness.ClaudeEffortChange:
			effort := command.Command.GetChangeReasoningEffort()
			if effort == nil || effort.Effort == "" {
				a.mu.Unlock()
				return fmt.Errorf("Claude effort request %q requires an effort command", request.ID)
			}
			a.effortChanges[request.ID] = claudeChange{commandID: id, value: effort.Effort}
		default:
			a.mu.Unlock()
			return fmt.Errorf("unexpected Claude request kind %q", request.Kind)
		}
		correlations[request.ID] = true
		before := uint64(0)
		if command.AdmittedCursor > 0 {
			before = command.AdmittedCursor - 1
		}
		if len(correlations) == 1 || before < replayFrom {
			replayFrom = before
		}
	}
	a.mu.Unlock()
	if len(correlations) == 0 {
		return nil
	}

	cursor := replayFrom
	for cursor < a.host.Journal().LastCursor() {
		entries, err := a.host.Journal().Since(ctx, cursor, 128)
		if err != nil {
			return err
		}
		if len(entries) == 0 {
			break
		}
		for _, entry := range entries {
			cursor = entry.GetCursor()
			native := entry.GetEvent().GetNative()
			if native == nil || native.GetDirection() != eventpb.Direction_DIRECTION_FROM_HARNESS {
				continue
			}
			var frame claudeWireFrame
			if json.Unmarshal([]byte(native.GetLine()), &frame) != nil {
				continue
			}
			matching := correlations[frame.UUID] || correlations[frame.UserMessageUUID] || correlations[frame.CommandUUID]
			if frame.Type == "control_response" {
				response, err := decodeClaudeControlResponse(json.RawMessage(native.GetLine()))
				if err == nil && correlations[response.RequestID] {
					matching = true
				}
			}
			if matching {
				if err := a.OnFrame(ctx, json.RawMessage(native.GetLine()), entry.GetOrigin().GetSequence()); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

func (a *claudeAdapter) Reconcile(ctx context.Context, turnID string, resumed bool) (*eventpb.ConversationReconciled, error) {
	observed, err := a.host.Journal().ObservedItems(ctx, turnID)
	if err != nil {
		return nil, err
	}
	var recovered map[string]harness.ObservedItem
	undetermined := make(map[string]bool)
	if resumed {
		a.mu.Lock()
		nativeID := a.nativeSessionID
		a.mu.Unlock()
		recovered, err = readClaudeHistory(filepath.Join(a.host.Session().Directory, "claude"), nativeID)
		if err != nil {
			return harness.UnknownReport(turnID, observed, "cannot inspect native continuation: "+err.Error()), nil
		}
	} else {
		answered, unparsed, err := a.answeredMessages(ctx, turnID)
		if err != nil {
			return nil, err
		}
		recovered = make(map[string]harness.ObservedItem)
		for itemID, item := range observed {
			messageID := itemID
			if separator := strings.LastIndexByte(itemID, '#'); separator >= 0 {
				messageID = itemID[:separator]
			}
			if item.Completed && (item.Kind != eventpb.ItemKind_ITEM_KIND_REASONING || answered[messageID]) {
				recovered[itemID] = item
			}
			if unparsed && item.Completed && item.Kind == eventpb.ItemKind_ITEM_KIND_REASONING && !answered[messageID] {
				undetermined[itemID] = true
			}
		}
	}

	report := &eventpb.ConversationReconciled{TurnId: turnID}
	for _, itemID := range slices.Sorted(maps.Keys(observed)) {
		item := observed[itemID]
		switch {
		case !resumed && item.Kind == eventpb.ItemKind_ITEM_KIND_TOOL_CALL && !item.Completed:
			report.Items = append(report.Items, harness.UnknownItem(item.ItemID, "Claude does not report whether it keeps a tool call interrupted before its result"))
		case undetermined[item.ItemID]:
			report.Items = append(report.Items, harness.UnknownItem(item.ItemID, "a native line of this turn could not be parsed, so whether another block of this message survived is unknown"))
		default:
			recoveredItem, ok := recovered[item.ItemID]
			if !ok {
				report.Items = append(report.Items, harness.CompareItem(item, nil))
			} else {
				report.Items = append(report.Items, harness.CompareItem(item, &recoveredItem))
			}
		}
	}
	return report, nil
}

func (a *claudeAdapter) answeredMessages(ctx context.Context, turnID string) (map[string]bool, bool, error) {
	entries, err := a.host.Journal().TurnEvents(ctx, turnID)
	if err != nil {
		return nil, false, err
	}
	messages := make([]claudeMessageWithBlocks, 0)
	unparsed := false
	for _, entry := range entries {
		if entry == nil || entry.Event == nil {
			continue
		}
		native := entry.Event.GetNative()
		if native == nil || native.Direction != eventpb.Direction_DIRECTION_FROM_HARNESS {
			continue
		}
		var frame claudeWireFrame
		if err := json.Unmarshal([]byte(native.Line), &frame); err != nil {
			unparsed = true
			continue
		}
		if frame.Type != "assistant" {
			continue
		}
		blocks, err := claudeBlocks(frame.Message.Content)
		if err != nil {
			unparsed = true
			continue
		}
		messageID := frame.Message.ID
		messages = append(messages, claudeMessageWithBlocks{Message: claudeMessage{ID: &messageID}, Blocks: blocks})
	}
	return answeredMessageIDs(messages), unparsed, nil
}

func (a *claudeAdapter) Submit(ctx context.Context, commandID, text string) error {
	if a.host.Session().ActiveTurnID == "" {
		turnID := newTurnID()
		if err := a.emit(ctx, &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: turnID, Model: a.host.Session().Model}}, nil); err != nil {
			return err
		}
	}
	messageUUID := uuid.NewString()
	if err := a.host.RecordNativeRequest(ctx, messageUUID, harness.ClaudeSubmit, []string{commandID}); err != nil {
		return err
	}
	frame, err := json.Marshal(map[string]any{
		"type": "user", "message": map[string]string{"role": "user", "content": text}, "parent_tool_use_id": nil, "uuid": messageUUID,
	})
	if err != nil {
		return err
	}
	a.mu.Lock()
	a.pending[messageUUID] = claudePendingInput{commandID: commandID, text: text}
	a.mu.Unlock()
	if err := a.host.SendNative(ctx, frame); err != nil {
		if !errors.Is(err, harness.ErrNativeResponsePending) {
			a.mu.Lock()
			delete(a.pending, messageUUID)
			a.mu.Unlock()
		}
		return err
	}
	return nil
}

func (a *claudeAdapter) Interrupt(ctx context.Context, turnID string) error {
	_ = turnID // Claude interrupts its current native turn; its protocol has no turn id on this request.
	requestID := nativeRequestID()
	frame, err := marshalClaudeControlRequest(requestID, claudeInterruptRequest{
		SDKControlInterruptRequest: types.SDKControlInterruptRequest{Subtype: "interrupt"},
		Reason:                     "agentplane", CancelQueued: false,
	})
	if err != nil {
		return err
	}
	return a.host.SendNative(ctx, frame)
}

func (a *claudeAdapter) ChangeModel(ctx context.Context, commandID, model string) error {
	requestID := nativeRequestID()
	a.mu.Lock()
	a.modelChanges[requestID] = claudeChange{commandID: commandID, value: model}
	a.mu.Unlock()
	if err := a.host.RecordNativeRequest(ctx, requestID, harness.ClaudeModelChange, []string{commandID}); err != nil {
		a.mu.Lock()
		delete(a.modelChanges, requestID)
		a.mu.Unlock()
		return err
	}
	err := a.sendControlRequest(ctx, requestID, claudeSetModelRequest{Subtype: "set_model", Model: model})
	if err != nil && !errors.Is(err, harness.ErrNativeResponsePending) {
		a.mu.Lock()
		delete(a.modelChanges, requestID)
		a.mu.Unlock()
	}
	return err
}

func (a *claudeAdapter) ChangeReasoningEffort(ctx context.Context, commandID, effort string) error {
	requestID := nativeRequestID()
	a.mu.Lock()
	a.effortChanges[requestID] = claudeChange{commandID: commandID, value: effort}
	a.mu.Unlock()
	if err := a.host.RecordNativeRequest(ctx, requestID, harness.ClaudeEffortChange, []string{commandID}); err != nil {
		a.mu.Lock()
		delete(a.effortChanges, requestID)
		a.mu.Unlock()
		return err
	}
	request := claudeApplyFlagSettingsRequest{Subtype: "apply_flag_settings"}
	request.Settings.EffortLevel = effort
	err := a.sendControlRequest(ctx, requestID, request)
	if err != nil && !errors.Is(err, harness.ErrNativeResponsePending) {
		a.mu.Lock()
		delete(a.effortChanges, requestID)
		a.mu.Unlock()
	}
	return err
}

func (a *claudeAdapter) sendControlRequest(ctx context.Context, requestID string, request any) error {
	frame, err := marshalClaudeControlRequest(requestID, request)
	if err != nil {
		return err
	}
	return a.host.SendNative(ctx, frame)
}

func (a *claudeAdapter) OnFrame(ctx context.Context, raw json.RawMessage, sourceSequence uint64) error {
	var frame claudeWireFrame
	if err := json.Unmarshal(raw, &frame); err != nil {
		return fmt.Errorf("decode Claude frame: %w", err)
	}
	sources := []uint64{sourceSequence}
	if frame.Type == "control_request" {
		var request types.SDKControlRequest
		if err := json.Unmarshal(raw, &request); err != nil {
			return err
		}
		return a.answerControlRequest(ctx, request)
	}
	if frame.ParentToolUseID != nil && (frame.Type == "system" || frame.Type == "stream_event" || frame.Type == "assistant" || frame.Type == "user" || frame.Type == "result") {
		return nil
	}
	if frame.Type == "system" && strings.HasPrefix(frame.Subtype, "task_") {
		return nil
	}

	a.mu.Lock()
	toolResult := a.toolResult != nil
	isStarted := frame.Type == "command_lifecycle" && frame.State == "started"
	isReplayPending := frame.Type == "user" && frame.IsReplay && hasString(a.pending, frame.UUID)
	a.mu.Unlock()
	if toolResult && !isStarted && !isReplayPending {
		if err := a.confirmToolResultInputs(ctx, sources); err != nil {
			return err
		}
	}

	switch frame.Type {
	case "control_response":
		response, err := decodeClaudeControlResponse(raw)
		if err != nil {
			return err
		}
		return a.handleControlResponse(ctx, response, sources)
	case "command_lifecycle":
		return a.handleCommandLifecycle(ctx, frame, sourceSequence, sources)
	case "stream_event":
		return a.handleStreamEvent(ctx, frame, sourceSequence, sources)
	case "assistant":
		return a.handleAssistant(ctx, frame, sources)
	case "user":
		return a.handleUser(ctx, frame, sourceSequence)
	case "result":
		return a.handleResult(ctx, frame, sources)
	default:
		// Unknown native frames remain archived verbatim by Session and have no runner projection.
		return nil
	}
}

func (a *claudeAdapter) handleControlResponse(ctx context.Context, response claudeControlResponseBody, sources []uint64) error {
	if response.Subtype != "success" && response.Subtype != "error" {
		return nil
	}
	a.mu.Lock()
	modelChange, hasModel := a.modelChanges[response.RequestID]
	effortChange, hasEffort := a.effortChanges[response.RequestID]
	if hasModel {
		delete(a.modelChanges, response.RequestID)
	}
	if hasEffort {
		delete(a.effortChanges, response.RequestID)
	}
	a.mu.Unlock()
	if hasModel {
		if response.Subtype == "success" {
			return a.host.ModelChanged(ctx, modelChange.commandID, modelChange.value, sources)
		}
		return a.host.FailCommand(ctx, modelChange.commandID, "Claude Code refused model switch: "+*response.Error, sources)
	}
	if hasEffort {
		if response.Subtype == "success" {
			return a.host.ReasoningEffortChanged(ctx, effortChange.commandID, effortChange.value, sources)
		}
		return a.host.FailCommand(ctx, effortChange.commandID, "Claude Code refused reasoning effort switch: "+*response.Error, sources)
	}
	return nil
}

func (a *claudeAdapter) handleCommandLifecycle(ctx context.Context, frame claudeWireFrame, sourceSequence uint64, sources []uint64) error {
	a.mu.Lock()
	switch frame.State {
	case "started":
		if _, ok := a.pending[frame.CommandUUID]; ok {
			a.started = append(a.started, claudeStartedInput{messageUUID: frame.CommandUUID, sequence: sourceSequence})
		}
		a.mu.Unlock()
		return nil
	case "cancelled":
		pending, ok := a.pending[frame.CommandUUID]
		if ok {
			delete(a.pending, frame.CommandUUID)
			started := a.started[:0]
			for _, input := range a.started {
				if input.messageUUID != frame.CommandUUID {
					started = append(started, input)
				}
			}
			a.started = started
		}
		a.mu.Unlock()
		if ok {
			return a.host.NoopCommand(ctx, pending.commandID, "Claude cancelled the queued user input before taking it", sources)
		}
		return nil
	}
	a.mu.Unlock()
	return nil
}

func (a *claudeAdapter) answerControlRequest(ctx context.Context, frame types.SDKControlRequest) error {
	var header struct {
		Subtype string `json:"subtype"`
	}
	if err := json.Unmarshal(frame.Request, &header); err != nil {
		return err
	}
	response := claudeControlResponseBody{RequestID: frame.RequestID}
	switch header.Subtype {
	case "can_use_tool":
		var request claudePermissionRequest
		if err := json.Unmarshal(frame.Request, &request); err != nil {
			return err
		}
		var toolInput map[string]json.RawMessage
		if len(request.Input) > 0 {
			if err := json.Unmarshal(request.Input, &toolInput); err != nil {
				return err
			}
		}
		payload, err := json.Marshal(claudePermissionAllow{Behavior: types.PermissionBehaviorAllow, UpdatedInput: request.Input})
		if err != nil {
			return err
		}
		response.Subtype, response.Response = "success", payload
	default:
		message := fmt.Sprintf("the agentplane runner does not answer %q requests", header.Subtype)
		response.Subtype, response.Error = "error", &message
	}
	raw, err := json.Marshal(response)
	if err != nil {
		return err
	}
	return a.send(ctx, types.SDKControlResponse{Type: "control_response", Response: raw})
}

func (a *claudeAdapter) handleStreamEvent(ctx context.Context, frame claudeWireFrame, sourceSequence uint64, sources []uint64) error {
	switch event := frame.Event.AsAny().(type) {
	case anthropic.MessageStartEvent:
		// SDK response decoding is lenient; preserve strict native-field validation.
		type fields anthropic.Message
		var message fields
		if err := json.Unmarshal([]byte(event.Message.RawJSON()), &message); err != nil {
			return err
		}
		if !event.Message.JSON.ID.Valid() {
			return errors.New("Claude message_start is missing message.id")
		}
		a.mu.Lock()
		a.messageID = event.Message.ID
		a.blockItems = make(map[int64]string)
		a.mu.Unlock()
		if frame.UserMessageUUID != "" {
			return a.confirmMessageStart(ctx, frame.UserMessageUUID, sourceSequence)
		}
	case anthropic.ContentBlockStartEvent:
		type fields anthropic.ContentBlockStartEvent
		var strict fields
		if err := json.Unmarshal([]byte(event.RawJSON()), &strict); err != nil {
			return err
		}
		if !event.JSON.Index.Valid() || !event.JSON.ContentBlock.Valid() {
			return errors.New("Claude content_block_start is missing index or content_block")
		}
		block, err := decodeClaudeBlock([]byte(event.ContentBlock.RawJSON()))
		if err != nil {
			return err
		}
		a.mu.Lock()
		messageID := a.messageID
		itemID := claudeItemID(block, int(event.Index), messageID)
		a.blockItems[event.Index] = itemID
		a.mu.Unlock()
		return a.startItem(ctx, itemID, block, sources)
	case anthropic.ContentBlockDeltaEvent:
		type fields anthropic.ContentBlockDeltaEvent
		var strict fields
		if err := json.Unmarshal([]byte(event.RawJSON()), &strict); err != nil {
			return err
		}
		if !event.JSON.Index.Valid() || !event.JSON.Delta.Valid() {
			return errors.New("Claude content_block_delta is missing index or delta")
		}
		a.mu.Lock()
		itemID := a.blockItems[event.Index]
		a.mu.Unlock()
		switch delta := event.Delta.AsAny().(type) {
		case anthropic.TextDelta:
			type fields anthropic.TextDelta
			var strict fields
			if err := json.Unmarshal([]byte(delta.RawJSON()), &strict); err != nil {
				return err
			}
			if !delta.JSON.Text.Valid() {
				return errors.New("Claude text_delta is missing text")
			}
			return a.emit(ctx, &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{ItemId: itemID, Text: delta.Text}}, sources)
		case anthropic.ThinkingDelta:
			type fields anthropic.ThinkingDelta
			var strict fields
			if err := json.Unmarshal([]byte(delta.RawJSON()), &strict); err != nil {
				return err
			}
			if !delta.JSON.Thinking.Valid() {
				return errors.New("Claude thinking_delta is missing thinking")
			}
			return a.emit(ctx, &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{ItemId: itemID, Text: delta.Thinking}}, sources)
		case anthropic.InputJSONDelta:
			type fields anthropic.InputJSONDelta
			var strict fields
			if err := json.Unmarshal([]byte(delta.RawJSON()), &strict); err != nil {
				return err
			}
			if !delta.JSON.PartialJSON.Valid() {
				return errors.New("Claude input_json_delta is missing partial_json")
			}
			return a.emit(ctx, &eventpb.Event_ToolArgumentsDelta{ToolArgumentsDelta: &eventpb.ToolArgumentsDelta{ItemId: itemID, PartialJson: delta.PartialJSON}}, sources)
		}
	}
	return nil
}

func (a *claudeAdapter) confirmMessageStart(ctx context.Context, messageUUID string, sourceSequence uint64) error {
	a.mu.Lock()
	started := append([]claudeStartedInput(nil), a.started...)
	if len(started) == 0 || started[len(started)-1].messageUUID != messageUUID {
		a.mu.Unlock()
		if containsStarted(started, messageUUID) {
			return errors.New("Claude started a model request for a non-representative input")
		}
		return nil
	}
	confirmed, err := a.consumeStartedLocked(started)
	a.mu.Unlock()
	if err != nil {
		return err
	}
	turnID, err := a.turnID(ctx, []uint64{sourceSequence})
	if err != nil {
		return err
	}
	sources := startedSequences(started, sourceSequence)
	return a.host.ConfirmUserMessage(ctx, harness.UserMessageConfirmation{
		HarnessMessageID: messageUUID, Text: joinTexts(confirmed), OriginCommandIDs: commandIDs(confirmed), TurnID: turnID,
	}, sources)
}

func (a *claudeAdapter) handleAssistant(ctx context.Context, frame claudeWireFrame, sources []uint64) error {
	blocks, err := claudeBlocks(frame.Message.Content)
	if err != nil {
		return err
	}
	for _, block := range blocks {
		index, err := a.host.Journal().NextAdapterBlock(ctx, a.adapterID, frame.Message.ID)
		if err != nil {
			return err
		}
		itemID := claudeHistoryItemID(block, index, frame.Message.ID)
		if itemID == "" {
			continue
		}
		has, err := a.host.Journal().HasAdapterItem(ctx, a.adapterID, itemID)
		if err != nil {
			return err
		}
		if !has {
			if err := a.startItem(ctx, itemID, block, sources); err != nil {
				return err
			}
		}
		switch block := block.AsAny().(type) {
		case anthropic.TextBlock:
			if err := a.emit(ctx, &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{ItemId: itemID, Outcome: &eventpb.ItemCompleted_Text{Text: block.Text}}}, sources); err != nil {
				return err
			}
		case anthropic.ThinkingBlock:
			if err := a.emit(ctx, &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{ItemId: itemID, Outcome: &eventpb.ItemCompleted_Text{Text: block.Thinking}}}, sources); err != nil {
				return err
			}
		case anthropic.ToolUseBlock:
			arguments := block.JSON.Input.Raw()
			if err := a.emit(ctx, &eventpb.Event_ToolArguments{ToolArguments: &eventpb.ToolArguments{ItemId: itemID, ArgumentsJson: arguments}}, sources); err != nil {
				return err
			}
		}
	}
	return nil
}

func (a *claudeAdapter) handleUser(ctx context.Context, frame claudeWireFrame, sourceSequence uint64) error {
	if frame.IsReplay {
		return a.confirmReplayedUserMessage(ctx, frame, sourceSequence)
	}
	results, err := claudeToolResults(frame.Message.Content)
	if err != nil {
		return err
	}
	hasToolResult := len(results) > 0
	for _, block := range results {
		output, err := block.resultText()
		if err != nil {
			return err
		}
		if err := a.emit(ctx, &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
			ItemId: block.ToolUseID, Outcome: &eventpb.ItemCompleted_Tool{Tool: &eventpb.ToolResult{Output: output, Succeeded: !block.IsError}},
		}}, []uint64{sourceSequence}); err != nil {
			return err
		}
	}
	if hasToolResult {
		a.mu.Lock()
		a.toolResult = &claudeToolResult{messageID: frame.UUID, sequence: sourceSequence}
		a.mu.Unlock()
	}
	return nil
}

func (a *claudeAdapter) confirmReplayedUserMessage(ctx context.Context, frame claudeWireFrame, sourceSequence uint64) error {
	a.mu.Lock()
	started := append([]claudeStartedInput(nil), a.started...)
	if len(started) == 0 || started[len(started)-1].messageUUID != frame.UUID {
		a.mu.Unlock()
		if containsStarted(started, frame.UUID) {
			return errors.New("Claude replayed a non-representative started input")
		}
		return nil
	}
	confirmed, err := a.peekStartedLocked(started)
	if err != nil {
		a.mu.Unlock()
		return err
	}
	text := joinTexts(confirmed)
	var replayed string
	if err := json.Unmarshal(frame.Message.Content, &replayed); err != nil || replayed != text {
		a.mu.Unlock()
		return errors.New("Claude replayed user text inconsistent with its started input batch")
	}
	confirmed, err = a.consumeStartedLocked(started)
	a.mu.Unlock()
	if err != nil {
		return err
	}
	turnID, err := a.turnID(ctx, []uint64{sourceSequence})
	if err != nil {
		return err
	}
	return a.host.ConfirmUserMessage(ctx, harness.UserMessageConfirmation{
		HarnessMessageID: frame.UUID, Text: text, OriginCommandIDs: commandIDs(confirmed), TurnID: turnID,
	}, startedSequences(started, sourceSequence))
}

func (a *claudeAdapter) confirmToolResultInputs(ctx context.Context, sources []uint64) error {
	a.mu.Lock()
	toolResult := a.toolResult
	a.toolResult = nil
	started := append([]claudeStartedInput(nil), a.started...)
	if toolResult == nil || len(started) == 0 {
		a.mu.Unlock()
		return nil
	}
	confirmed, err := a.consumeStartedLocked(started)
	a.mu.Unlock()
	if err != nil {
		return err
	}
	turnID, err := a.turnID(ctx, sources)
	if err != nil {
		return err
	}
	causal := []uint64{toolResult.sequence}
	for _, input := range started {
		causal = append(causal, input.sequence)
	}
	return a.host.ConfirmUserMessage(ctx, harness.UserMessageConfirmation{
		HarnessMessageID: toolResult.messageID, Text: joinTexts(confirmed), OriginCommandIDs: commandIDs(confirmed), TurnID: turnID,
	}, causal)
}

func (a *claudeAdapter) handleResult(ctx context.Context, frame claudeWireFrame, sources []uint64) error {
	session := a.host.Session()
	if session.ActiveTurnID == "" {
		return nil
	}
	status := eventpb.TurnStatus_TURN_STATUS_COMPLETED
	errorText := ""
	if strings.HasPrefix(frame.TerminalReason, "aborted") {
		status = eventpb.TurnStatus_TURN_STATUS_INTERRUPTED
	} else if frame.IsError {
		status = eventpb.TurnStatus_TURN_STATUS_FAILED
		errorText = frame.Result
	}
	return a.emit(ctx, &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{TurnId: session.ActiveTurnID, Status: status, Error: errorText}}, sources)
}

func (a *claudeAdapter) startItem(ctx context.Context, itemID string, block anthropic.ContentBlockUnion, sources []uint64) error {
	var kind eventpb.ItemKind
	var toolName string
	switch block := block.AsAny().(type) {
	case anthropic.TextBlock:
		kind = eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT
	case anthropic.ThinkingBlock:
		kind = eventpb.ItemKind_ITEM_KIND_REASONING
	case anthropic.ToolUseBlock:
		kind, toolName = eventpb.ItemKind_ITEM_KIND_TOOL_CALL, block.Name
	default:
		return nil
	}
	if _, err := a.host.Journal().RememberAdapterItem(ctx, a.adapterID, itemID); err != nil {
		return err
	}
	if _, err := a.turnID(ctx, sources); err != nil {
		return err
	}
	return a.emit(ctx, &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: itemID, Kind: kind, ToolName: toolName}}, sources)
}

func (a *claudeAdapter) turnID(ctx context.Context, sources []uint64) (string, error) {
	if turnID := a.host.Session().ActiveTurnID; turnID != "" {
		return turnID, nil
	}
	turnID := newTurnID()
	if err := a.emit(ctx, &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: turnID, Model: a.host.Session().Model}}, sources); err != nil {
		return "", err
	}
	if active := a.host.Session().ActiveTurnID; active != "" {
		return active, nil
	}
	return turnID, nil
}

func (a *claudeAdapter) emit(ctx context.Context, observation any, sources []uint64) error {
	event := &eventpb.Event{SourceSequences: append([]uint64(nil), sources...)}
	switch value := observation.(type) {
	case *eventpb.Event_TurnStarted:
		event.Observation = value
	case *eventpb.Event_TurnCompleted:
		event.Observation = value
	case *eventpb.Event_ItemStarted:
		event.Observation = value
	case *eventpb.Event_ItemCompleted:
		event.Observation = value
	case *eventpb.Event_TextDelta:
		event.Observation = value
	case *eventpb.Event_ToolArgumentsDelta:
		event.Observation = value
	case *eventpb.Event_ToolArguments:
		event.Observation = value
	default:
		return fmt.Errorf("unsupported Claude observation type %T", observation)
	}
	return a.host.Emit(ctx, event, sources)
}

func (a *claudeAdapter) send(ctx context.Context, value any) error {
	frame, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return a.host.SendNative(ctx, frame)
}

func (a *claudeAdapter) consumeStartedLocked(started []claudeStartedInput) ([]claudePendingInput, error) {
	confirmed, err := a.peekStartedLocked(started)
	if err != nil {
		return nil, err
	}
	for _, input := range started {
		delete(a.pending, input.messageUUID)
	}
	a.started = nil
	return confirmed, nil
}

func (a *claudeAdapter) peekStartedLocked(started []claudeStartedInput) ([]claudePendingInput, error) {
	confirmed := make([]claudePendingInput, 0, len(started))
	for _, input := range started {
		pending, ok := a.pending[input.messageUUID]
		if !ok {
			return nil, errors.New("Claude started an input the runner did not have pending")
		}
		confirmed = append(confirmed, pending)
	}
	return confirmed, nil
}

func claudeItemID(block anthropic.ContentBlockUnion, index int, messageID string) string {
	if tool, ok := block.AsAny().(anthropic.ToolUseBlock); ok {
		return tool.ID
	}
	return fmt.Sprintf("%s#%d", messageID, index)
}

func claudeHistoryItemID(block anthropic.ContentBlockUnion, index int, messageID string) string {
	switch block.AsAny().(type) {
	case anthropic.TextBlock, anthropic.ThinkingBlock, anthropic.ToolUseBlock:
		return claudeItemID(block, index, messageID)
	default:
		return ""
	}
}

func nativeRequestID() string {
	return "capture-" + strings.ReplaceAll(uuid.NewString(), "-", "")
}

func newTurnID() string {
	return "turn-" + strings.ReplaceAll(uuid.NewString(), "-", "")
}

func joinTexts(inputs []claudePendingInput) string {
	texts := make([]string, 0, len(inputs))
	for _, input := range inputs {
		texts = append(texts, input.text)
	}
	return strings.Join(texts, "\n")
}

func commandIDs(inputs []claudePendingInput) []string {
	ids := make([]string, 0, len(inputs))
	for _, input := range inputs {
		ids = append(ids, input.commandID)
	}
	return ids
}

func startedSequences(inputs []claudeStartedInput, final uint64) []uint64 {
	sequences := make([]uint64, 0, len(inputs)+1)
	for _, input := range inputs {
		sequences = append(sequences, input.sequence)
	}
	return append(sequences, final)
}

func containsStarted(inputs []claudeStartedInput, uuid string) bool {
	for _, input := range inputs {
		if input.messageUUID == uuid {
			return true
		}
	}
	return false
}

func hasString(inputs map[string]claudePendingInput, key string) bool {
	_, ok := inputs[key]
	return ok
}

var _ harness.ProcessAdapter = (*claudeAdapter)(nil)
