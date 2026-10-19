package codex

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

	"github.com/agentydragon/ducktape/agentplane/runner/go/harness"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
)

const (
	codexMaxRetries = 2
	codexClientName = "agentplane"
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
	host   harness.AdapterHost
	launch harness.LaunchConfig

	mu                   sync.Mutex
	threadID             string
	adapterID            string
	nextRequest          uint64
	pendingModelChanges  []codexPendingChange
	pendingEffortChanges []codexPendingChange
	turnStarts           map[string]codexTurnStart
}

// New creates an adapter bound to one runner-owned Session.
func New(host harness.AdapterHost, launch harness.LaunchConfig) harness.Adapter {
	snapshot := host.Session()
	threadID := ""
	if snapshot.Resuming {
		threadID = snapshot.NativeSessionID
	}
	return &codexAdapter{
		host: host, launch: launch, threadID: threadID, adapterID: uuid.NewString(),
		turnStarts: make(map[string]codexTurnStart),
	}
}

func (a *codexAdapter) Command() ([]string, error) {
	session := a.host.Session()
	args := []string{a.launch.Binary}
	appendOverride := func(key string, value any) error {
		encoded, err := json.Marshal(value)
		if err != nil {
			return err
		}
		args = append(args, "-c", key+"="+string(encoded))
		return nil
	}
	overrides := []struct {
		key   string
		value any
	}{
		{"model_provider", "agentplane"},
		{"model_providers.agentplane.name", "Agentplane LiteLLM"},
		{"model_providers.agentplane.base_url", a.launch.BaseURL},
		{"model_providers.agentplane.env_key", "OPENAI_API_KEY"},
		{"model_providers.agentplane.wire_api", "responses"},
		{"model_providers.agentplane.request_max_retries", 0},
		{"model_providers.agentplane.stream_max_retries", codexMaxRetries},
		{"approval_policy", "never"},
		{"sandbox_mode", "danger-full-access"},
		{"skills.bundled.enabled", false},
		{"skills.include_instructions", false},
		{"include_permissions_instructions", false},
		{"include_apps_instructions", false},
		{"include_collaboration_mode_instructions", false},
		{"include_environment_context", false},
		{"web_search", "disabled"},
		{"features.multi_agent", true},
		{"features.view_image", false},
		{"features.unbounded_connection_retries", false},
	}
	for _, override := range overrides {
		if err := appendOverride(override.key, override.value); err != nil {
			return nil, err
		}
	}
	if session.TotalContextBudgetTokens != nil {
		if *session.TotalContextBudgetTokens <= 0 {
			return nil, fmt.Errorf("model_context_window must be positive")
		}
		if err := appendOverride("model_context_window", *session.TotalContextBudgetTokens); err != nil {
			return nil, err
		}
	}
	return append(args, "app-server", "--listen", "stdio://"), nil
}

func (a *codexAdapter) Environment() (map[string]string, error) {
	codexHome := filepath.Join(a.host.Session().Directory, "codex")
	if err := os.MkdirAll(codexHome, 0o700); err != nil {
		return nil, err
	}
	environment := make(map[string]string)
	maps.Copy(environment, a.host.Session().HarnessEnvironment)
	environment["CODEX_HOME"] = codexHome
	environment["OPENAI_API_KEY"] = a.launch.Token
	environment["OPENAI_BASE_URL"] = a.launch.BaseURL
	return environment, nil
}

func (a *codexAdapter) Handshake(ctx context.Context) (string, error) {
	initializeID, err := a.nextID()
	if err != nil {
		return "", err
	}
	response, _, err := a.request(ctx, initializeID, map[string]any{
		"method": "initialize", "id": initializeID,
		"params": map[string]any{
			"clientInfo": map[string]string{"name": codexClientName, "version": "0.1"}, "capabilities": nil,
		},
	})
	if err != nil {
		return "", fmt.Errorf("Codex initialize: %w", err)
	}
	if err := codexResponseError(response); err != nil {
		return "", fmt.Errorf("Codex initialize failed: %w", err)
	}
	if err := a.send(ctx, map[string]any{"method": "initialized"}); err != nil {
		return "", err
	}

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
		response, _, err = a.request(ctx, requestID, map[string]any{
			"method": method, "id": requestID,
			"params": map[string]any{"threadId": threadID, "excludeTurns": true},
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
		params := map[string]any{
			"cwd": snapshot.Cwd, "approvalPolicy": "never", "sandbox": "danger-full-access",
			"ephemeral": false, "model": snapshot.Model,
			"config": map[string]any{"model_reasoning_effort": snapshot.ReasoningEffort},
		}
		if snapshot.Instructions != "" {
			params["developerInstructions"] = snapshot.Instructions
		}
		response, _, err = a.request(ctx, requestID, map[string]any{"method": method, "id": requestID, "params": params})
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
	var threadResult struct {
		Thread struct {
			ID string `json:"id"`
		} `json:"thread"`
	}
	if err := json.Unmarshal(result, &threadResult); err != nil {
		return "", err
	}
	if threadResult.Thread.ID == "" {
		return "", fmt.Errorf("Codex %s returned no thread id", method)
	}
	a.mu.Lock()
	a.threadID = threadResult.Thread.ID
	a.mu.Unlock()
	return threadResult.Thread.ID, nil
}

func (a *codexAdapter) Reconcile(ctx context.Context, turnID string, _ bool) (*eventpb.ConversationReconciled, error) {
	observed, err := a.host.Journal().ObservedItems(ctx, turnID)
	if err != nil {
		return nil, err
	}
	a.mu.Lock()
	threadID := a.threadID
	a.mu.Unlock()
	recovered, err := readCodexHistory(filepath.Join(a.host.Session().Directory, "codex"), threadID, observed)
	if err != nil {
		return harness.UnknownReport(turnID, observed, "cannot inspect native continuation: "+err.Error()), nil
	}
	if recovered == nil {
		return harness.UnknownReport(turnID, observed, "native continuation evidence is unavailable or unsupported"), nil
	}
	report := &eventpb.ConversationReconciled{TurnId: turnID}
	for _, itemID := range slices.Sorted(maps.Keys(observed)) {
		item := observed[itemID]
		if item.Kind == eventpb.ItemKind_ITEM_KIND_TOOL_CALL && item.ToolName != "commandExecution" {
			report.Items = append(report.Items, harness.UnknownItem(item.ItemID, fmt.Sprintf("the adapter does not match %s items to saved function calls", item.ToolName)))
			continue
		}
		recoveredItem, ok := recovered[itemID]
		if !ok {
			report.Items = append(report.Items, harness.CompareItem(item, nil))
		} else {
			report.Items = append(report.Items, harness.CompareItem(item, &recoveredItem))
		}
	}
	return report, nil
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
	a.mu.Lock()
	a.turnStarts[requestID] = start
	a.mu.Unlock()
	response, sourceSequence, err := a.request(ctx, requestID, map[string]any{
		"method": "turn/start", "id": requestID,
		"params": map[string]any{
			"threadId": threadID, "input": []any{map[string]any{"type": "text", "text": text, "text_elements": []any{}}},
			"model": model, "effort": effort,
		},
	})
	if err != nil {
		a.mu.Lock()
		delete(a.turnStarts, requestID)
		a.mu.Unlock()
		return err
	}
	// The native reader also dispatches this receipt to OnFrame. The pending request is removed
	// atomically there, so calling it here closes the gap if the response waiter won the race.
	return a.onResponse(ctx, response, requestID, []uint64{sourceSequence}, sourceSequence)
}

func (a *codexAdapter) Interrupt(ctx context.Context, turnID string) error {
	a.mu.Lock()
	threadID := a.threadID
	a.mu.Unlock()
	requestID, err := a.nextID()
	if err != nil {
		return err
	}
	_, _, err = a.request(ctx, requestID, map[string]any{
		"method": "turn/interrupt", "id": requestID,
		"params": map[string]string{"threadId": threadID, "turnId": turnID},
	})
	return err
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
	var frame codexWireFrame
	if err := json.Unmarshal(raw, &frame); err != nil {
		return fmt.Errorf("decode Codex frame: %w", err)
	}
	sources := []uint64{sourceSequence}
	if len(frame.ID) > 0 && len(frame.Method) > 0 {
		return a.answerServerRequest(ctx, frame)
	}
	threadScoped := map[string]bool{
		"turn/started": true, "turn/completed": true, "item/started": true, "item/completed": true,
		"item/agentMessage/delta": true, "item/reasoning/summaryTextDelta": true,
		"item/commandExecution/outputDelta": true,
	}
	if threadScoped[frame.Method] {
		a.mu.Lock()
		threadID := a.threadID
		a.mu.Unlock()
		if frame.Params.ThreadID != threadID {
			return nil
		}
	}
	if len(frame.ID) > 0 && len(frame.Method) == 0 {
		id, ok := jsonString(frame.ID)
		if ok {
			return a.onResponse(ctx, frame, id, sources, sourceSequence)
		}
		return nil
	}
	switch frame.Method {
	case "turn/started":
		turnID := frame.Params.Turn.ID
		if turnID != a.host.Session().ActiveTurnID {
			return a.emit(ctx, &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: turnID, Model: a.host.Session().Model}}, sources)
		}
	case "turn/completed":
		turn := frame.Params.Turn
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
		return a.projectItemStarted(ctx, frame.Params.Item, sources)
	case "item/completed":
		return a.projectItemCompleted(ctx, frame.Params.Item, sources)
	case "item/agentMessage/delta", "item/reasoning/summaryTextDelta":
		return a.emit(ctx, &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{ItemId: frame.Params.ItemID, Text: frame.Params.Delta}}, sources)
	case "item/commandExecution/outputDelta":
		return a.emit(ctx, &eventpb.Event_ToolOutputDelta{ToolOutputDelta: &eventpb.ToolOutputDelta{ItemId: frame.Params.ItemID, Text: frame.Params.Delta}}, sources)
	}
	return nil
}

func (a *codexAdapter) onResponse(ctx context.Context, frame codexWireFrame, requestID string, sources []uint64, sequence uint64) error {
	a.mu.Lock()
	start, ok := a.turnStarts[requestID]
	if ok {
		delete(a.turnStarts, requestID)
	}
	a.mu.Unlock()
	if !ok {
		return nil
	}
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
	var result struct {
		Turn struct {
			ID string `json:"id"`
		} `json:"turn"`
	}
	if err := json.Unmarshal(frame.Result, &result); err != nil {
		return err
	}
	if result.Turn.ID == "" {
		return errors.New("Codex turn/start returned no turn id")
	}
	if result.Turn.ID != a.host.Session().ActiveTurnID {
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
	if item.Type == "userMessage" || (item.Type == "subAgentActivity" && item.Kind != "started" && item.Kind != "interacted" && item.Kind != "interrupted" && item.Kind != "completed") {
		return nil
	}
	remembered, err := a.host.Journal().RememberAdapterItem(ctx, a.adapterID, item.ID)
	if err != nil || !remembered {
		return err
	}
	switch item.Type {
	case "agentMessage":
		return a.emit(ctx, &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: item.ID, Kind: eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT}}, sources)
	case "reasoning":
		return a.emit(ctx, &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: item.ID, Kind: eventpb.ItemKind_ITEM_KIND_REASONING}}, sources)
	case "commandExecution", "subAgentActivity":
		if err := a.emit(ctx, &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: item.ID, Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: item.Type}}, sources); err != nil {
			return err
		}
		var arguments any
		if item.Type == "commandExecution" {
			arguments = map[string]any{"command": item.Command, "cwd": item.Cwd}
		} else {
			arguments = struct {
				AgentThreadID string `json:"agentThreadId"`
				AgentPath     string `json:"agentPath"`
			}{AgentThreadID: item.AgentThreadID, AgentPath: item.AgentPath}
		}
		return a.toolArguments(ctx, item.ID, arguments, sources)
	case "unknown":
		if err := a.emit(ctx, &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: item.ID, Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: item.WireType}}, sources); err != nil {
			return err
		}
		arguments := make(map[string]json.RawMessage)
		for key, value := range item.Extras {
			if !codexOutcomeFields[key] {
				arguments[key] = value
			}
		}
		return a.toolArguments(ctx, item.ID, arguments, sources)
	}
	return nil
}

func (a *codexAdapter) projectItemCompleted(ctx context.Context, raw json.RawMessage, sources []uint64) error {
	item, err := decodeCodexItem(raw)
	if err != nil {
		return err
	}
	if item.Type == "userMessage" || (item.Type == "subAgentActivity" && item.Kind != "started" && item.Kind != "interacted" && item.Kind != "interrupted" && item.Kind != "completed") {
		return nil
	}
	if err := a.projectItemStarted(ctx, raw, sources); err != nil {
		return err
	}
	switch item.Type {
	case "agentMessage":
		return a.itemCompletedText(ctx, item.ID, item.Text, sources)
	case "reasoning":
		return a.itemCompletedText(ctx, item.ID, strings.Join(item.Summary, "\n"), sources)
	case "commandExecution":
		return a.itemCompletedTool(ctx, item.ID, item.AggregatedOutput, item.Status == "completed", sources)
	case "subAgentActivity":
		output, err := json.Marshal(struct {
			Kind          string `json:"kind"`
			AgentThreadID string `json:"agentThreadId"`
			AgentPath     string `json:"agentPath"`
		}{Kind: item.Kind, AgentThreadID: item.AgentThreadID, AgentPath: item.AgentPath})
		if err != nil {
			return err
		}
		return a.itemCompletedTool(ctx, item.ID, string(output), true, sources)
	case "unknown":
		outcome := make(map[string]json.RawMessage)
		for key, value := range item.Extras {
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
		return a.itemCompletedTool(ctx, item.ID, string(output), status == "completed", sources)
	}
	return nil
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

func (a *codexAdapter) answerServerRequest(ctx context.Context, frame codexWireFrame) error {
	return a.send(ctx, map[string]any{
		"id": frame.ID, "error": map[string]any{
			"code": -32601, "message": "the agentplane runner does not answer " + frame.Method, "data": nil,
		},
	})
}

func (a *codexAdapter) request(ctx context.Context, requestID string, request any) (codexWireFrame, uint64, error) {
	frame, err := json.Marshal(request)
	if err != nil {
		return codexWireFrame{}, 0, err
	}
	receipt, err := a.host.RequestNative(ctx, frame, func(raw json.RawMessage) bool {
		var response codexWireFrame
		return json.Unmarshal(raw, &response) == nil && len(response.ID) > 0 && len(response.Method) == 0 && rawJSONIDEquals(response.ID, requestID)
	})
	if err != nil {
		return codexWireFrame{}, 0, err
	}
	var response codexWireFrame
	if err := json.Unmarshal(receipt.Frame, &response); err != nil {
		return codexWireFrame{}, 0, err
	}
	return response, receipt.Sequence, nil
}

func (a *codexAdapter) nextID() (string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.nextRequest++
	return fmt.Sprintf("agentplane-%d", a.nextRequest), nil
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

type codexWireFrame struct {
	ID     json.RawMessage `json:"id"`
	Method string          `json:"method"`
	Result json.RawMessage `json:"result"`
	Error  *struct {
		Code    int             `json:"code"`
		Message string          `json:"message"`
		Data    json.RawMessage `json:"data"`
	} `json:"error"`
	Params struct {
		ThreadID string `json:"threadId"`
		TurnID   string `json:"turnId"`
		Turn     struct {
			ID     string `json:"id"`
			Status string `json:"status"`
			Error  *struct {
				Message string `json:"message"`
			} `json:"error"`
		} `json:"turn"`
		Item   json.RawMessage `json:"item"`
		ItemID string          `json:"itemId"`
		Delta  string          `json:"delta"`
	} `json:"params"`
}

type codexItem struct {
	Type             string
	WireType         string
	ID               string
	Text             string
	Summary          []string
	Command          string
	Cwd              string
	Status           string
	AggregatedOutput string
	Kind             string
	AgentThreadID    string
	AgentPath        string
	Extras           map[string]json.RawMessage
}

func decodeCodexItem(raw json.RawMessage) (codexItem, error) {
	var item codexItem
	var values map[string]json.RawMessage
	if err := json.Unmarshal(raw, &values); err != nil {
		return item, err
	}
	if err := json.Unmarshal(values["type"], &item.WireType); err != nil {
		return item, err
	}
	if err := json.Unmarshal(values["id"], &item.ID); err != nil {
		return item, err
	}
	item.Type = item.WireType
	switch item.WireType {
	case "userMessage":
	case "agentMessage":
		if err := json.Unmarshal(values["text"], &item.Text); err != nil {
			return item, err
		}
	case "reasoning":
		var summary []string
		if len(values["summary"]) > 0 {
			if err := json.Unmarshal(values["summary"], &summary); err != nil {
				return item, err
			}
		}
		item.Summary = summary
	case "commandExecution":
		for key, destination := range map[string]*string{"command": &item.Command, "cwd": &item.Cwd, "status": &item.Status} {
			if len(values[key]) > 0 {
				if err := json.Unmarshal(values[key], destination); err != nil {
					return item, err
				}
			}
		}
		if len(values["aggregatedOutput"]) > 0 && string(values["aggregatedOutput"]) != "null" {
			if err := json.Unmarshal(values["aggregatedOutput"], &item.AggregatedOutput); err != nil {
				return item, err
			}
		}
	case "subAgentActivity":
		for key, destination := range map[string]*string{"kind": &item.Kind, "agentThreadId": &item.AgentThreadID, "agentPath": &item.AgentPath} {
			if err := json.Unmarshal(values[key], destination); err != nil {
				return item, err
			}
		}
	default:
		item.Type = "unknown"
		item.Extras = values
		delete(item.Extras, "type")
		delete(item.Extras, "id")
	}
	return item, nil
}

func rawJSONIDEquals(raw json.RawMessage, expected string) bool {
	var value string
	return json.Unmarshal(raw, &value) == nil && value == expected
}

func jsonString(raw json.RawMessage) (string, bool) {
	var value string
	err := json.Unmarshal(raw, &value)
	return value, err == nil
}

func codexResponseError(frame codexWireFrame) error {
	if frame.Error != nil {
		return fmt.Errorf("%s", frame.Error.Message)
	}
	return nil
}

var _ harness.Adapter = (*codexAdapter)(nil)
