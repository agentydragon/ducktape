package chatcompletions

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerio"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

const Path = "/v1/chat/completions"

func Headers() map[string]string { return map[string]string{"content-type": "application/json"} }

type Result struct {
	Record        json.RawMessage
	Audit         Audit
	ContextTokens *int
}

type Audit struct {
	ID           string          `json:"id"`
	FinishReason string          `json:"finish_reason"`
	Usage        json.RawMessage `json:"usage,omitempty"`
}

type chunk struct {
	ID      string          `json:"id"`
	Error   json.RawMessage `json:"error"`
	Usage   json.RawMessage `json:"usage"`
	Choices []struct {
		Index        int    `json:"index"`
		FinishReason string `json:"finish_reason"`
		Delta        struct {
			Role      string `json:"role"`
			Content   string `json:"content"`
			Reasoning string `json:"reasoning_content"`
			ToolCalls []struct {
				Index *int `json:"index"`
				toolCall
			} `json:"tool_calls"`
		} `json:"delta"`
	} `json:"choices"`
}

type frame struct {
	Data json.RawMessage `json:"data,omitempty"`
	Done bool            `json:"done,omitempty"`
}

func (frame) Kind() eventpb.ExecutionProgress_Kind { return eventpb.ExecutionProgress_KIND_PROVIDER }

func Sample(ctx context.Context, client *http.Client, baseURL, authorization string, body []byte, evidence protocol.EvidenceSink, attribution map[string]string, compaction bool, hooks protocol.ProviderHooks) (*Result, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(baseURL, "/")+Path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	for name, value := range Headers() {
		request.Header.Set(name, value)
	}
	for name, value := range attribution {
		request.Header.Set(name, value)
	}
	if authorization != "" {
		request.Header.Set("Authorization", authorization)
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, providerio.ProviderTransportError(ctx, err)
	}
	defer response.Body.Close()
	head := protocol.HTTPResponseHead{
		HTTPStatus: response.StatusCode, ProviderRequestID: response.Header.Get("x-request-id"), ContentType: response.Header.Get("content-type"),
	}
	if err := protocol.RecordProviderEvidence(ctx, evidence, head); err != nil {
		return nil, err
	}
	if hooks.ResponseHeadRecorded != nil {
		hooks.ResponseHeadRecorded()
	}
	reader := protocol.CaptureReader{Reader: response.Body, Context: ctx, Evidence: evidence}
	if response.StatusCode != http.StatusOK {
		body, err := io.ReadAll(io.LimitReader(reader, 1024*1024))
		if err != nil {
			return nil, providerio.ProviderTransportError(ctx, err)
		}
		return nil, providerio.ProviderStatusError(response, "provider", map[string]any{"http_status": response.StatusCode, "body": string(body)})
	}
	return readStream(ctx, io.LimitReader(reader, protocol.MaxProviderStreamBytes), evidence, compaction, hooks)
}

type stream struct {
	compaction    bool
	audit         Audit
	text          strings.Builder
	reasoning     strings.Builder
	calls         []toolCall
	arguments     []*strings.Builder
	contextTokens *int
}

func readStream(ctx context.Context, reader io.Reader, evidence protocol.EvidenceSink, compaction bool, hooks protocol.ProviderHooks) (*Result, error) {
	state := stream{compaction: compaction}
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 16*1024), protocol.MaxProviderStreamBytes)
	var lines []string
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "data:") {
			lines = append(lines, strings.TrimPrefix(line[5:], " "))
			continue
		}
		if line != "" || len(lines) == 0 {
			continue
		}
		data := strings.Join(lines, "\n")
		lines = lines[:0]
		if data == "[DONE]" {
			return state.finish(ctx, evidence)
		}
		progress, err := state.consume(ctx, []byte(data), evidence)
		if err != nil {
			return nil, err
		}
		if progress && hooks.Progress != nil {
			if err := hooks.Progress(ctx); err != nil {
				return nil, err
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, providerio.ProviderTransportError(ctx, err)
	}
	return nil, providerio.ProviderTransportError(ctx, errors.New("Chat Completions stream ended before [DONE]"))
}

func (s *stream) consume(ctx context.Context, raw json.RawMessage, evidence protocol.EvidenceSink) (bool, error) {
	var c chunk
	if !bytes.HasPrefix(bytes.TrimSpace(raw), []byte("{")) || json.Unmarshal(raw, &c) != nil {
		return false, malformed("invalid streamed JSON object")
	}
	var observations []*eventpb.Event
	if len(c.Error) > 0 && string(c.Error) != "null" {
		if err := protocol.RecordProviderEvidence(ctx, evidence, frame{Data: raw}); err != nil {
			return false, err
		}
		return false, &protocol.KnownFailureError{Evidence: map[string]any{"error": c.Error}}
	}
	if c.ID != "" {
		if s.audit.ID != "" && s.audit.ID != c.ID {
			return false, malformed("response ID changed mid-stream")
		}
		s.audit.ID = c.ID
	}
	if len(c.Usage) > 0 && string(c.Usage) != "null" {
		var usage struct {
			TotalTokens *int `json:"total_tokens"`
		}
		if err := json.Unmarshal(c.Usage, &usage); err != nil {
			return false, malformed("invalid usage")
		}
		if usage.TotalTokens != nil && *usage.TotalTokens >= 0 {
			s.contextTokens = usage.TotalTokens
		}
		s.audit.Usage = c.Usage
	}
	progress := false
	if len(c.Choices) > 0 {
		if len(c.Choices) != 1 || c.Choices[0].Index != 0 {
			return false, malformed("unexpected choice")
		}
		if s.audit.FinishReason != "" {
			if !isTrailingUsageChoice(raw, c.Usage) {
				return false, malformed("unexpected choice")
			}
			return false, s.record(ctx, evidence, frame{Data: raw}, nil)
		}
		choice := c.Choices[0]
		d := choice.Delta
		if s.audit.ID == "" || (d.Role != "" && d.Role != "assistant") {
			return false, malformed("missing response ID or invalid role")
		}
		if d.Content != "" {
			if s.text.Len() == 0 {
				observations = append(observations, startItem(s.audit.ID, eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT, ""))
			}
			observations = append(observations, &eventpb.Event{Observation: &eventpb.Event_TextDelta{
				TextDelta: &eventpb.TextDelta{ItemId: s.audit.ID, Text: protocol.DisplayText(d.Content)},
			}})
			s.text.WriteString(d.Content)
		}
		s.reasoning.WriteString(d.Reasoning)
		for _, fragment := range d.ToolCalls {
			if fragment.Index == nil || *fragment.Index < 0 || *fragment.Index > len(s.calls) {
				return false, malformed("invalid tool call index")
			}
			i := *fragment.Index
			if i == len(s.calls) {
				s.calls = append(s.calls, toolCall{})
				s.arguments = append(s.arguments, &strings.Builder{})
			}
			call := &s.calls[i]
			if !mergeStable(&call.ID, fragment.ID) || !mergeStable(&call.Type, fragment.Type) {
				return false, malformed("tool call identity changed")
			}
			call.Function.Name += fragment.Function.Name
			s.arguments[i].WriteString(fragment.Function.Arguments)
		}
		s.audit.FinishReason = choice.FinishReason
		progress = d.Content != "" || d.Reasoning != "" || len(d.ToolCalls) > 0 || choice.FinishReason != ""
	}
	return progress, s.record(ctx, evidence, frame{Data: raw}, observations)
}

// LiteLLM can attach a usage-only chunk to an empty choice after the finish
// marker. Reject any unknown choice or delta fields so that later content is
// never silently discarded.
func isTrailingUsageChoice(raw, usage json.RawMessage) bool {
	if len(usage) == 0 || bytes.Equal(bytes.TrimSpace(usage), []byte("null")) {
		return false
	}
	var body struct {
		Choices []map[string]json.RawMessage `json:"choices"`
	}
	if err := json.Unmarshal(raw, &body); err != nil || len(body.Choices) != 1 {
		return false
	}
	choice := body.Choices[0]
	if !bytes.Equal(bytes.TrimSpace(choice["index"]), []byte("0")) {
		return false
	}
	for key := range choice {
		if key != "index" && key != "delta" && key != "finish_reason" {
			return false
		}
	}
	if finish, ok := choice["finish_reason"]; ok && !bytes.Equal(bytes.TrimSpace(finish), []byte("null")) {
		return false
	}
	delta := bytes.TrimSpace(choice["delta"])
	if len(delta) == 0 || delta[0] != '{' {
		return false
	}
	var fields map[string]json.RawMessage
	return json.Unmarshal(delta, &fields) == nil && len(fields) == 0
}

func mergeStable(dst *string, value string) bool {
	if value == "" {
		return true
	}
	if *dst != "" && *dst != value {
		return false
	}
	*dst = value
	return true
}

func (s *stream) finish(ctx context.Context, evidence protocol.EvidenceSink) (*Result, error) {
	for i := range s.calls {
		s.calls[i].Function.Arguments = s.arguments[i].String()
	}
	m := message{Role: "assistant", Content: s.text.String(), Reasoning: s.reasoning.String(), ToolCalls: s.calls}
	switch s.audit.FinishReason {
	case "tool_calls":
		if len(s.calls) == 0 {
			// TabbyAPI can report this finish reason even when its tool parser yields no calls.
			slog.WarnContext(ctx, "Chat Completions finished with tool_calls but sent no tool calls", "response_id", s.audit.ID)
			return nil, &protocol.ProviderTransientError{Cause: errors.New("invalid Chat Completions stream: tool_calls finish without any tool calls")}
		}
	case "stop":
		if len(s.calls) > 0 {
			return nil, malformed("finish reason does not match tool calls")
		}
	default:
		return nil, &protocol.KnownFailureError{Evidence: map[string]any{"finish_reason": s.audit.FinishReason}}
	}
	if _, _, err := inspect([]message{m}); err != nil {
		return nil, malformed(err.Error())
	}
	record, err := json.Marshal(m)
	if err != nil {
		return nil, err
	}
	var observations []*eventpb.Event
	if m.Content != "" {
		observations = append(observations, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{
			ItemCompleted: &eventpb.ItemCompleted{ItemId: s.audit.ID, Outcome: &eventpb.ItemCompleted_Text{Text: protocol.DisplayText(m.Content)}},
		}})
	}
	for _, call := range m.ToolCalls {
		args := call.Function.Arguments
		if len(args) > protocol.DisplayBytes {
			args = `{"notice":"Tool arguments exceed the display limit; see provider evidence."}`
		}
		observations = append(observations, startItem(call.ID, eventpb.ItemKind_ITEM_KIND_TOOL_CALL, call.Function.Name),
			&eventpb.Event{Observation: &eventpb.Event_ToolArguments{
				ToolArguments: &eventpb.ToolArguments{ItemId: call.ID, ArgumentsJson: args},
			}})
	}
	if err := s.record(ctx, evidence, frame{Done: true}, observations); err != nil {
		return nil, err
	}
	return &Result{Record: record, Audit: s.audit, ContextTokens: s.contextTokens}, nil
}

func (s *stream) record(ctx context.Context, evidence protocol.EvidenceSink, frame frame, observations []*eventpb.Event) error {
	if s.compaction {
		observations = nil
	}
	return protocol.RecordProviderEvidence(ctx, evidence, frame, observations...)
}

func startItem(id string, kind eventpb.ItemKind, toolName string) *eventpb.Event {
	return &eventpb.Event{Observation: &eventpb.Event_ItemStarted{
		ItemStarted: &eventpb.ItemStarted{ItemId: id, Kind: kind, ToolName: toolName},
	}}
}

func malformed(reason string) error {
	return &protocol.KnownFailureError{Evidence: map[string]any{"reason": reason}, Message: "invalid Chat Completions stream: " + reason}
}
