// Package testsupport contains helpers shared by central sampler tests.
package testsupport

import (
	"context"
	"encoding/json"
	"log/slog"
	"sync"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionconfig"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

// FillSessionConfigurationDefaults supplies fixture identity, executor, and placement when absent.
func FillSessionConfigurationDefaults(request *sessionconfig.Configuration) {
	if request.GetExecutionIdentity() == nil {
		request.ExecutionIdentity = &identitypb.Identity{Name: "fixture-session-identity", Uid: "fixture-session-identity-uid"}
	}
	if request.GetExecutor() == nil {
		request.Executor = &identitypb.SandboxDestination{
			Owner:  &identitypb.ServiceAccount{Namespace: "fixture", Name: "executor"},
			Target: &identitypb.EnvironmentTarget{Namespace: "fixture", Name: "fixture-sandbox", Uid: "fixture-sandbox-uid"},
		}
	}
	if request.GetSpec() != nil && request.GetSpec().GetPlacement() == nil {
		request.Spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{
			ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: request.GetExecutor().GetTarget().GetName(), Uid: request.GetExecutor().GetTarget().GetUid()},
		}}}
	}
}

type capturedLog map[string]any

type logCapture struct {
	mu      sync.Mutex
	records []capturedLog
}

func (c *logCapture) Enabled(context.Context, slog.Level) bool { return true }

func (c *logCapture) Handle(_ context.Context, record slog.Record) error {
	fields := capturedLog{"msg": record.Message, "level": record.Level.String()}
	record.Attrs(func(attr slog.Attr) bool {
		fields[attr.Key] = attr.Value.Any()
		return true
	})
	c.mu.Lock()
	defer c.mu.Unlock()
	c.records = append(c.records, fields)
	return nil
}

func (c *logCapture) WithAttrs([]slog.Attr) slog.Handler { return c }
func (c *logCapture) WithGroup(string) slog.Handler      { return c }

// CaptureLogs routes the default logger into the test until cleanup and returns
// the first record with a requested message.
func CaptureLogs(t testing.TB) func(message string) map[string]any {
	t.Helper()
	capture := &logCapture{}
	previous := slog.Default()
	slog.SetDefault(slog.New(capture))
	t.Cleanup(func() { slog.SetDefault(previous) })
	return func(message string) map[string]any {
		t.Helper()
		capture.mu.Lock()
		defer capture.mu.Unlock()
		for _, record := range capture.records {
			if record["msg"] == message {
				return record
			}
		}
		t.Fatalf("no log record %q among %d records", message, len(capture.records))
		return nil
	}
}

// NativeResponsesRecords JSON-encodes provider items for the native context API.
func NativeResponsesRecords(t testing.TB, items ...responses.Item) []json.RawMessage {
	t.Helper()
	records := make([]json.RawMessage, len(items))
	for index, item := range items {
		encoded, err := json.Marshal(item)
		if err != nil {
			t.Fatalf("marshal native Responses record %d: %v", index+1, err)
		}
		records[index] = encoded
	}
	return records
}

// ResponsesItems decodes a Responses protocol context into provider items.
func ResponsesItems(t testing.TB, state protocol.Context) []responses.Item {
	t.Helper()
	if state == nil || state.API() != protocol.APIStyleResponses {
		t.Fatalf("native context is %T, want Responses", state)
	}
	records, err := state.Records()
	if err != nil {
		t.Fatal(err)
	}
	items := make([]responses.Item, 0, len(records))
	for _, raw := range records {
		var item responses.Item
		if err := json.Unmarshal(raw, &item); err != nil {
			t.Fatal(err)
		}
		items = append(items, item)
	}
	return items
}
