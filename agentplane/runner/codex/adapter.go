package codex

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/runner/codex/protocol"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/jsonrpc"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
)

const (
	codexMaxRetries = 2
	codexClientName = "agentplane"
	// The LLM ingress route serving Codex's model catalog. It is not under the provider's `/v1`.
	codexCatalogPath = "/agentplane/codex/models"
)

var codexOutcomeFields = map[string]bool{
	"status": true, "aggregatedOutput": true, "exitCode": true,
	"durationMs": true, "processId": true, "agentsStates": true,
}

type codexPendingChange struct {
	commandID string
	value     string
}

type codexTurnStart struct {
	commandID    string
	text         string
	modelChange  *codexPendingChange
	effortChange *codexPendingChange
}

type codexAdapter struct {
	historyMu      sync.Mutex
	host           harness.AdapterHost
	historyMode    *protocol.ThreadHistoryMode
	resumedHistory *codexHistorySnapshot

	mu                   sync.Mutex
	threadID             string
	adapterID            string
	pendingModelChanges  []codexPendingChange
	pendingEffortChanges []codexPendingChange
	turnStarts           map[string]codexTurnStart
	processingResponses  map[string]bool
}

// New creates an adapter for one runner-owned Session hosted as a thread of the Runner's shared
// app-server. Native history is read through that server's public RPCs.
func New(host harness.AdapterHost) harness.Adapter {
	snapshot := host.Session()
	threadID := ""
	if snapshot.Resuming {
		threadID = snapshot.NativeSessionID
	}
	return &codexAdapter{
		host: host, threadID: threadID, adapterID: uuid.NewString(),
		turnStarts: make(map[string]codexTurnStart), processingResponses: make(map[string]bool),
	}
}

func (a *codexAdapter) BlocksCommandDispatch() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.turnStarts) != 0
}

// Handshake starts or resumes this Session's thread on the shared app-server, which the Runner has
// already initialized.
func (a *codexAdapter) Handshake(ctx context.Context) (string, error) {
	var response jsonrpc.Message
	snapshot := a.host.Session()
	a.mu.Lock()
	threadID := a.threadID
	a.mu.Unlock()
	method := "thread/start"
	var result json.RawMessage
	if threadID != "" {
		method = "thread/resume"
		requestID, err := a.nextID()
		if err != nil {
			return "", err
		}
		excludeTurns := true
		// Codex replays the old developer message on resume but rebuilds it from
		// this configuration at the next compaction.
		instructions := snapshot.Instructions
		response, err = a.request(ctx, jsonrpc.Request[any]{
			ID: requestID, Method: method,
			Params: protocol.ThreadResumeParams{
				ThreadID: threadID, ExcludeTurns: &excludeTurns, Config: threadEnvironment(snapshot.Environment),
				DeveloperInstructions: &instructions,
			},
		})
		if err != nil {
			return "", fmt.Errorf("Codex thread/resume: %w", err)
		}
		if err := codexResponseError(response); err != nil {
			return "", fmt.Errorf("Codex refused thread/resume: %w", err)
		}
		result = response.Result
	} else {
		requestID, err := a.nextID()
		if err != nil {
			return "", err
		}
		never := protocol.Never
		sandbox := protocol.SandboxMode("danger-full-access")
		ephemeral := false
		params := protocol.ThreadStartParams{
			Cwd: &snapshot.Cwd, ApprovalPolicy: &protocol.ApprovalPolicyUnion{Enum: &never}, Sandbox: &sandbox,
			Ephemeral: &ephemeral, Model: &snapshot.Model,
			Config: threadEnvironment(snapshot.Environment),
		}
		params.Config["model_reasoning_effort"] = snapshot.ReasoningEffort
		if snapshot.Instructions != "" {
			params.DeveloperInstructions = &snapshot.Instructions
		}
		response, err = a.request(ctx, jsonrpc.Request[any]{ID: requestID, Method: method, Params: params})
		if err != nil {
			return "", fmt.Errorf("Codex thread/start: %w", err)
		}
		if err := codexResponseError(response); err != nil {
			return "", fmt.Errorf("Codex refused thread/start: %w", err)
		}
		result = response.Result
	}
	if len(result) == 0 || string(result) == "null" {
		return "", fmt.Errorf("Codex %s returned no result", method)
	}
	var nativeThreadID string
	var historyMode *protocol.ThreadHistoryMode
	if method == "thread/resume" {
		var response protocol.ThreadResumeResponse
		if err := json.Unmarshal(result, &response); err != nil {
			return "", err
		}
		nativeThreadID = response.Thread.ID
		historyMode = response.Thread.HistoryMode
	} else {
		var response protocol.ThreadStartResponse
		if err := json.Unmarshal(result, &response); err != nil {
			return "", err
		}
		nativeThreadID = response.Thread.ID
		historyMode = response.Thread.HistoryMode
	}
	if nativeThreadID == "" {
		return "", fmt.Errorf("Codex %s returned no thread id", method)
	}
	a.mu.Lock()
	a.threadID = nativeThreadID
	a.historyMode = historyMode
	a.mu.Unlock()
	return nativeThreadID, nil
}

func (a *codexAdapter) Recover(ctx context.Context) error {
	pending, err := a.host.Journal().PendingCommandRecords(ctx)
	if err != nil {
		return err
	}
	requests, err := a.host.Journal().PendingNativeRequests(ctx)
	if err != nil {
		return err
	}
	requestIDs := map[string]bool{}
	inFlightChanges := map[string]bool{}
	var replayFrom uint64
	a.mu.Lock()
	for _, request := range requests {
		if request.Kind != harness.CodexTurnStart {
			a.mu.Unlock()
			return fmt.Errorf("unexpected Codex request kind %q", request.Kind)
		}
		start := codexTurnStart{}
		for _, record := range request.Commands {
			command := record.Command
			if input := command.GetSubmitInput(); input != nil {
				start.commandID, start.text = command.GetCommandId(), input.GetText()
				before := uint64(0)
				if record.AdmittedCursor > 0 {
					before = record.AdmittedCursor - 1
				}
				if len(requestIDs) == 0 || before < replayFrom {
					replayFrom = before
				}
			}
			inFlightChanges[command.GetCommandId()] = true
			if record.TerminalCursor != nil {
				continue
			}
			if model := command.GetChangeModel(); model != nil {
				start.modelChange = &codexPendingChange{commandID: command.GetCommandId(), value: model.GetModel()}
			}
			if effort := command.GetChangeReasoningEffort(); effort != nil {
				start.effortChange = &codexPendingChange{commandID: command.GetCommandId(), value: effort.GetEffort()}
			}
		}
		if start.commandID == "" || start.text == "" {
			a.mu.Unlock()
			return fmt.Errorf("Codex turn/start request %q requires an input command", request.ID)
		}
		a.turnStarts[request.ID] = start
		requestIDs[request.ID] = true
	}
	for _, record := range pending {
		if !record.DispatchPlanned || inFlightChanges[record.Command.GetCommandId()] {
			continue
		}
		command := record.Command
		if model := command.GetChangeModel(); model != nil {
			a.pendingModelChanges = append(a.pendingModelChanges, codexPendingChange{commandID: command.GetCommandId(), value: model.GetModel()})
		}
		if effort := command.GetChangeReasoningEffort(); effort != nil {
			a.pendingEffortChanges = append(a.pendingEffortChanges, codexPendingChange{commandID: command.GetCommandId(), value: effort.GetEffort()})
		}
	}
	a.mu.Unlock()
	if len(requestIDs) == 0 {
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
			frame, err := jsonrpc.Parse([]byte(native.GetLine()))
			if err != nil || !frame.IsResponse() {
				continue
			}
			requestID, ok := frame.StringID()
			if !ok || !requestIDs[requestID] {
				continue
			}
			sequence := entry.GetOrigin().GetSequence()
			if err := a.onResponse(ctx, frame, requestID, []uint64{sequence}); err != nil {
				return err
			}
			delete(requestIDs, requestID)
		}
	}
	// Recovery uses journaled replies to settle old requests. Unanswered commands stay
	// uncertain; only requests sent by this adapter can block dispatch awaiting a live reply.
	a.mu.Lock()
	for requestID := range requestIDs {
		delete(a.turnStarts, requestID)
	}
	a.mu.Unlock()
	return nil
}

func (a *codexAdapter) Submit(ctx context.Context, commandID, text string) error {
	modelChange, err := a.takePendingChange(ctx, &a.pendingModelChanges, "model")
	if err != nil {
		return err
	}
	effortChange, err := a.takePendingChange(ctx, &a.pendingEffortChanges, "effort")
	if err != nil {
		return err
	}
	snapshot := a.host.Session()
	model := snapshot.Model
	if modelChange != nil {
		model = modelChange.value
	}
	effort := snapshot.ReasoningEffort
	if effortChange != nil {
		effort = effortChange.value
	}
	a.mu.Lock()
	threadID := a.threadID
	a.mu.Unlock()
	if threadID == "" {
		return errors.New("Codex thread is not initialized")
	}
	requestID, err := a.nextID()
	if err != nil {
		return err
	}
	start := codexTurnStart{commandID: commandID, text: text, modelChange: modelChange, effortChange: effortChange}
	commandIDs := []string{commandID}
	if modelChange != nil {
		commandIDs = append(commandIDs, modelChange.commandID)
	}
	if effortChange != nil {
		commandIDs = append(commandIDs, effortChange.commandID)
	}
	if err := a.host.RecordNativeRequest(ctx, requestID, harness.CodexTurnStart, commandIDs); err != nil {
		return err
	}
	a.mu.Lock()
	a.turnStarts[requestID] = start
	a.mu.Unlock()
	if err := a.send(ctx, jsonrpc.Request[any]{
		ID: requestID, Method: "turn/start",
		Params: protocol.TurnStartParams{
			ThreadID: threadID, Input: []protocol.UserInput{{Type: "text", Text: &text}},
			Model: &model, Effort: &effort,
		},
	}); err != nil {
		if !errors.Is(err, harness.ErrNativeResponsePending) {
			a.mu.Lock()
			delete(a.turnStarts, requestID)
			a.mu.Unlock()
		}
		return err
	}
	// The command remains pending until OnFrame observes the journaled turn/start response.
	return nil
}

func (a *codexAdapter) Interrupt(ctx context.Context, turnID string) error {
	a.mu.Lock()
	threadID := a.threadID
	a.mu.Unlock()
	requestID, err := a.nextID()
	if err != nil {
		return err
	}
	return a.send(ctx, jsonrpc.Request[any]{
		ID: requestID, Method: "turn/interrupt",
		Params: protocol.TurnInterruptParams{ThreadID: threadID, TurnID: turnID},
	})
}

func (a *codexAdapter) ChangeModel(_ context.Context, commandID, model string) error {
	a.mu.Lock()
	a.pendingModelChanges = append(a.pendingModelChanges, codexPendingChange{commandID: commandID, value: model})
	a.mu.Unlock()
	return nil
}

func (a *codexAdapter) ChangeReasoningEffort(_ context.Context, commandID, effort string) error {
	a.mu.Lock()
	a.pendingEffortChanges = append(a.pendingEffortChanges, codexPendingChange{commandID: commandID, value: effort})
	a.mu.Unlock()
	return nil
}

func (a *codexAdapter) takePendingChange(ctx context.Context, changes *[]codexPendingChange, label string) (*codexPendingChange, error) {
	a.mu.Lock()
	var pending []codexPendingChange
	switch changes {
	case &a.pendingModelChanges:
		pending = a.pendingModelChanges
		a.pendingModelChanges = nil
	case &a.pendingEffortChanges:
		pending = a.pendingEffortChanges
		a.pendingEffortChanges = nil
	default:
		a.mu.Unlock()
		return nil, errors.New("unexpected Codex pending-change queue")
	}
	a.mu.Unlock()
	if len(pending) == 0 {
		return nil, nil
	}
	for _, superseded := range pending[:len(pending)-1] {
		reason := fmt.Sprintf("superseded by a later %s command before any Codex turn selected it", label)
		if err := a.host.NoopCommand(ctx, superseded.commandID, reason, nil); err != nil {
			return nil, err
		}
	}
	selected := pending[len(pending)-1]
	return &selected, nil
}

func (a *codexAdapter) OnFrame(ctx context.Context, raw json.RawMessage, sourceSequence uint64) error {
	frame, err := jsonrpc.Parse(raw)
	if err != nil {
		return fmt.Errorf("decode Codex frame: %w", err)
	}
	sources := []uint64{sourceSequence}
	if frame.IsRequest() {
		return a.answerServerRequest(ctx, frame)
	}
	threadScoped := map[string]bool{
		"turn/started": true, "turn/completed": true, "item/started": true, "item/completed": true,
		"item/agentMessage/delta": true, "item/reasoning/summaryTextDelta": true,
		"item/commandExecution/outputDelta": true,
	}
	var routing map[string]json.RawMessage
	if threadScoped[frame.Method] {
		if err := json.Unmarshal(frame.Params, &routing); err != nil {
			return err
		}
		var scopedThreadID string
		if rawID := routing["threadId"]; len(rawID) != 0 {
			if err := json.Unmarshal(rawID, &scopedThreadID); err != nil {
				return err
			}
		}
		a.mu.Lock()
		threadID := a.threadID
		a.mu.Unlock()
		if scopedThreadID != threadID {
			return nil
		}
	}
	if frame.IsResponse() {
		if id, ok := frame.StringID(); ok {
			return a.onResponse(ctx, frame, id, sources)
		}
		return nil
	}
	switch frame.Method {
	case "turn/started":
		var params protocol.TurnStartedNotification
		if err := json.Unmarshal(frame.Params, &params); err != nil {
			return err
		}
		turnID := params.Turn.ID
		if turnID != a.host.Session().ActiveTurnID {
			return a.emit(ctx, &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: turnID, Model: a.host.Session().Model}}, sources)
		}
	case "turn/completed":
		var params protocol.TurnCompletedNotification
		if err := json.Unmarshal(frame.Params, &params); err != nil {
			return err
		}
		turn := params.Turn
		status := eventpb.TurnStatus_TURN_STATUS_UNSPECIFIED
		errorText := ""
		knownStatus := true
		switch turn.Status {
		case "completed":
			status = eventpb.TurnStatus_TURN_STATUS_COMPLETED
		case "interrupted":
			status = eventpb.TurnStatus_TURN_STATUS_INTERRUPTED
		case "failed":
			status = eventpb.TurnStatus_TURN_STATUS_FAILED
		default:
			status = eventpb.TurnStatus_TURN_STATUS_FAILED
			errorText = fmt.Sprintf("the turn ended with an unrecognized status %q", turn.Status)
			knownStatus = false
		}
		if knownStatus && turn.Error != nil {
			errorText = turn.Error.Message
		}
		return a.emit(ctx, &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{TurnId: turn.ID, Status: status, Error: errorText}}, sources)
	case "item/started":
		return a.projectItemStarted(ctx, routing["item"], sources)
	case "item/completed":
		return a.projectItemCompleted(ctx, routing["item"], sources)
	case "item/agentMessage/delta":
		var params protocol.AgentMessageDeltaNotification
		if err := json.Unmarshal(frame.Params, &params); err != nil {
			return err
		}
		return a.emit(ctx, &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{ItemId: params.ItemID, Text: params.Delta}}, sources)
	case "item/reasoning/summaryTextDelta":
		var params protocol.ReasoningSummaryTextDeltaNotification
		if err := json.Unmarshal(frame.Params, &params); err != nil {
			return err
		}
		return a.emit(ctx, &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{ItemId: params.ItemID, Text: params.Delta}}, sources)
	case "item/commandExecution/outputDelta":
		var params protocol.CommandExecutionOutputDeltaNotification
		if err := json.Unmarshal(frame.Params, &params); err != nil {
			return err
		}
		return a.emit(ctx, &eventpb.Event_ToolOutputDelta{ToolOutputDelta: &eventpb.ToolOutputDelta{ItemId: params.ItemID, Text: params.Delta}}, sources)
	}
	return nil
}

func (a *codexAdapter) onResponse(ctx context.Context, frame jsonrpc.Message, requestID string, sources []uint64) error {
	a.mu.Lock()
	start, ok := a.turnStarts[requestID]
	if !ok || a.processingResponses[requestID] {
		a.mu.Unlock()
		return nil
	}
	a.processingResponses[requestID] = true
	a.mu.Unlock()
	err := a.applyTurnStartResponse(ctx, frame, start, sources)
	a.mu.Lock()
	delete(a.processingResponses, requestID)
	if err == nil {
		delete(a.turnStarts, requestID)
	}
	a.mu.Unlock()
	return err
}

func (a *codexAdapter) applyTurnStartResponse(ctx context.Context, frame jsonrpc.Message, start codexTurnStart, sources []uint64) error {
	if frame.Error != nil || len(frame.Result) == 0 || string(frame.Result) == "null" {
		reason := "turn/start returned no result"
		if frame.Error != nil {
			reason = frame.Error.Message
		}
		if start.modelChange != nil {
			if err := a.host.FailCommand(ctx, start.modelChange.commandID, "Codex did not select the requested model: "+reason, sources); err != nil {
				return err
			}
		}
		if start.effortChange != nil {
			if err := a.host.FailCommand(ctx, start.effortChange.commandID, "Codex did not select the requested effort: "+reason, sources); err != nil {
				return err
			}
		}
		return a.host.FailCommand(ctx, start.commandID, reason, sources)
	}
	if start.modelChange != nil {
		if err := a.host.ModelChanged(ctx, start.modelChange.commandID, start.modelChange.value, sources); err != nil {
			return err
		}
	}
	if start.effortChange != nil {
		if err := a.host.ReasoningEffortChanged(ctx, start.effortChange.commandID, start.effortChange.value, sources); err != nil {
			return err
		}
	}
	var result protocol.TurnStartResponse
	if err := json.Unmarshal(frame.Result, &result); err != nil {
		return err
	}
	if result.Turn.ID == "" {
		return errors.New("Codex turn/start returned no turn id")
	}
	started := false
	turnEvents, err := a.host.Journal().TurnEvents(ctx, result.Turn.ID)
	if err != nil && !errors.Is(err, harness.ErrUnknownRecoveryTurn) {
		return err
	}
	for _, entry := range turnEvents {
		if entry.GetEvent().GetTurnStarted() != nil {
			started = true
			break
		}
	}
	if !started && result.Turn.ID != a.host.Session().ActiveTurnID {
		if err := a.emit(ctx, &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: result.Turn.ID, Model: a.host.Session().Model}}, sources); err != nil {
			return err
		}
	}
	return a.host.ConfirmUserMessage(ctx, harness.UserMessageConfirmation{
		HarnessMessageID: result.Turn.ID, Text: start.text, OriginCommandIDs: []string{start.commandID}, TurnID: result.Turn.ID,
	}, sources)
}

func (a *codexAdapter) projectItemStarted(ctx context.Context, raw json.RawMessage, sources []uint64) error {
	item, err := decodeCodexItem(raw)
	if err != nil {
		return err
	}
	switch item := item.(type) {
	case nil:
		return nil
	case protocol.AgentMessageThreadItem:
		_, err := a.startItem(ctx, item.ID, eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT, "", sources)
		return err
	case protocol.ReasoningThreadItem:
		_, err := a.startItem(ctx, item.ID, eventpb.ItemKind_ITEM_KIND_REASONING, "", sources)
		return err
	case protocol.CommandExecutionThreadItem:
		started, err := a.startItem(ctx, item.ID, eventpb.ItemKind_ITEM_KIND_TOOL_CALL, "commandExecution", sources)
		if err != nil || !started {
			return err
		}
		return a.toolArguments(ctx, item.ID, map[string]any{"command": item.Command, "cwd": item.Cwd}, sources)
	case protocol.SubAgentActivityThreadItem:
		kind := string(item.Kind)
		if kind != "started" && kind != "interacted" && kind != "interrupted" && kind != "completed" {
			return nil
		}
		started, err := a.startItem(ctx, item.ID, eventpb.ItemKind_ITEM_KIND_TOOL_CALL, "subAgentActivity", sources)
		if err != nil || !started {
			return err
		}
		return a.toolArguments(ctx, item.ID, struct {
			AgentThreadID string `json:"agentThreadId"`
			AgentPath     string `json:"agentPath"`
		}{AgentThreadID: item.AgentThreadID, AgentPath: item.AgentPath}, sources)
	case codexUnknownItem:
		fields, err := item.fields()
		if err != nil {
			return err
		}
		started, err := a.startItem(ctx, item.id, eventpb.ItemKind_ITEM_KIND_TOOL_CALL, item.typeName, sources)
		if err != nil || !started {
			return err
		}
		arguments := make(map[string]json.RawMessage)
		for key, value := range fields {
			if !codexOutcomeFields[key] {
				arguments[key] = value
			}
		}
		return a.toolArguments(ctx, item.id, arguments, sources)
	default:
		return fmt.Errorf("unsupported decoded Codex item type %T", item)
	}
}

func (a *codexAdapter) projectItemCompleted(ctx context.Context, raw json.RawMessage, sources []uint64) error {
	item, err := decodeCodexItem(raw)
	if err != nil {
		return err
	}
	if item == nil {
		return nil
	}
	if err := a.projectItemStarted(ctx, raw, sources); err != nil {
		return err
	}
	switch item := item.(type) {
	case protocol.AgentMessageThreadItem:
		return a.itemCompletedText(ctx, item.ID, item.Text, sources)
	case protocol.ReasoningThreadItem:
		return a.itemCompletedText(ctx, item.ID, strings.Join(item.Summary, "\n"), sources)
	case protocol.CommandExecutionThreadItem:
		output := ""
		if item.AggregatedOutput != nil {
			output = *item.AggregatedOutput
		}
		return a.itemCompletedTool(ctx, item.ID, output, string(item.Status) == "completed", sources)
	case protocol.SubAgentActivityThreadItem:
		kind := string(item.Kind)
		if kind != "started" && kind != "interacted" && kind != "interrupted" && kind != "completed" {
			return nil
		}
		output, err := json.Marshal(struct {
			Kind          string `json:"kind"`
			AgentThreadID string `json:"agentThreadId"`
			AgentPath     string `json:"agentPath"`
		}{Kind: kind, AgentThreadID: item.AgentThreadID, AgentPath: item.AgentPath})
		if err != nil {
			return err
		}
		return a.itemCompletedTool(ctx, item.ID, string(output), true, sources)
	case codexUnknownItem:
		fields, err := item.fields()
		if err != nil {
			return err
		}
		outcome := make(map[string]json.RawMessage)
		for key, value := range fields {
			if codexOutcomeFields[key] {
				outcome[key] = value
			}
		}
		status := ""
		_ = json.Unmarshal(outcome["status"], &status)
		output, err := json.Marshal(outcome)
		if err != nil {
			return err
		}
		return a.itemCompletedTool(ctx, item.id, string(output), status == "completed", sources)
	default:
		return fmt.Errorf("unsupported decoded Codex item type %T", item)
	}
}

func (a *codexAdapter) startItem(ctx context.Context, itemID string, kind eventpb.ItemKind, toolName string, sources []uint64) (bool, error) {
	remembered, err := a.host.Journal().RememberAdapterItem(ctx, a.adapterID, itemID)
	if err != nil || !remembered {
		return false, err
	}
	return true, a.emit(ctx, &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
		ItemId: itemID, Kind: kind, ToolName: toolName,
	}}, sources)
}

func (a *codexAdapter) toolArguments(ctx context.Context, itemID string, value any, sources []uint64) error {
	encoded, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return a.emit(ctx, &eventpb.Event_ToolArguments{ToolArguments: &eventpb.ToolArguments{ItemId: itemID, ArgumentsJson: string(encoded)}}, sources)
}

func (a *codexAdapter) itemCompletedText(ctx context.Context, itemID, text string, sources []uint64) error {
	return a.emit(ctx, &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{ItemId: itemID, Outcome: &eventpb.ItemCompleted_Text{Text: text}}}, sources)
}

func (a *codexAdapter) itemCompletedTool(ctx context.Context, itemID, output string, succeeded bool, sources []uint64) error {
	return a.emit(ctx, &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
		ItemId: itemID, Outcome: &eventpb.ItemCompleted_Tool{Tool: &eventpb.ToolResult{Output: output, Succeeded: succeeded}},
	}}, sources)
}

func (a *codexAdapter) answerServerRequest(ctx context.Context, frame jsonrpc.Message) error {
	return a.send(ctx, jsonrpc.ErrorReply{
		ID:    frame.ID,
		Error: jsonrpc.Error{Code: -32601, Message: "the agentplane runner does not answer " + frame.Method},
	})
}

func (a *codexAdapter) request(ctx context.Context, request jsonrpc.Request[any]) (jsonrpc.Message, error) {
	frame, err := json.Marshal(request)
	if err != nil {
		return jsonrpc.Message{}, err
	}
	receipt, err := a.host.RequestNativeHandshake(ctx, frame, func(raw json.RawMessage) bool {
		response, err := jsonrpc.Parse(raw)
		return err == nil && response.Answers(request.ID)
	})
	if err != nil {
		return jsonrpc.Message{}, err
	}
	return jsonrpc.Parse(receipt.Frame)
}

// nextID names a request uniquely on the shared app-server, which carries every Codex Session's requests.
func (a *codexAdapter) nextID() (string, error) {
	return "agentplane-" + uuid.NewString(), nil
}

func (a *codexAdapter) send(ctx context.Context, value any) error {
	frame, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return a.host.SendNative(ctx, frame)
}

func (a *codexAdapter) emit(ctx context.Context, observation any, sources []uint64) error {
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
	case *eventpb.Event_ToolArguments:
		event.Observation = value
	case *eventpb.Event_ToolOutputDelta:
		event.Observation = value
	default:
		return fmt.Errorf("unsupported Codex observation type %T", observation)
	}
	return a.host.Emit(ctx, event, sources)
}

type codexUnknownItem struct {
	typeName string
	id       string
	raw      json.RawMessage
}

func (item codexUnknownItem) fields() (map[string]json.RawMessage, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(item.raw, &fields); err != nil {
		return nil, err
	}
	delete(fields, "type")
	delete(fields, "id")
	return fields, nil
}

// decodeCodexItem returns the generated type for known items and keeps unknown items opaque.
func decodeCodexItem(raw json.RawMessage) (any, error) {
	var header struct {
		Type string `json:"type"`
		ID   string `json:"id"`
	}
	if err := json.Unmarshal(raw, &header); err != nil {
		return nil, err
	}
	switch header.Type {
	case "userMessage":
		return nil, nil
	case "agentMessage":
		var native protocol.AgentMessageThreadItem
		if err := json.Unmarshal(raw, &native); err != nil {
			return nil, err
		}
		return native, nil
	case "reasoning":
		var native protocol.ReasoningThreadItem
		if err := json.Unmarshal(raw, &native); err != nil {
			return nil, err
		}
		return native, nil
	case "commandExecution":
		var native protocol.CommandExecutionThreadItem
		if err := json.Unmarshal(raw, &native); err != nil {
			return nil, err
		}
		return native, nil
	case "subAgentActivity":
		var native protocol.SubAgentActivityThreadItem
		if err := json.Unmarshal(raw, &native); err != nil {
			return nil, err
		}
		return native, nil
	default:
		return codexUnknownItem{
			typeName: header.Type,
			id:       header.ID,
			raw:      append(json.RawMessage(nil), raw...),
		}, nil
	}
}

func codexResponseError(frame jsonrpc.Message) error {
	if frame.Error != nil {
		return fmt.Errorf("%s", frame.Error.Message)
	}
	return nil
}

var _ harness.Adapter = (*codexAdapter)(nil)
