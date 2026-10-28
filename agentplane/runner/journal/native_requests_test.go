package journal

import (
	"context"
	"testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
)

func TestNativeRequestRegistrationRollsBackOnCommandReuse(t *testing.T) {
	j := openTestJournal(t)
	ctx := context.Background()
	for _, command := range []*commandpb.Command{
		{CommandId: "model-1", Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: "test-model"}}},
		{CommandId: "input-1", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}}},
	} {
		if _, _, err := j.Admit(ctx, command, ""); err != nil {
			t.Fatal(err)
		}
		if _, err := j.DispatchPlanned(ctx, command.CommandId); err != nil {
			t.Fatal(err)
		}
	}
	if err := j.RecordNativeRequest(ctx, "model-request", harness.ClaudeModelChange, []string{"model-1"}); err != nil {
		t.Fatal(err)
	}
	// The input link is inserted before the reused model link fails; both it and the new request roll back.
	if err := j.RecordNativeRequest(ctx, "turn-request", harness.CodexTurnStart, []string{"input-1", "model-1"}); err == nil {
		t.Fatal("command was attached to two native requests")
	}
	var requests, links int
	if err := j.conn.QueryRowContext(ctx, "SELECT COUNT(*) FROM native_request").Scan(&requests); err != nil {
		t.Fatal(err)
	}
	if err := j.conn.QueryRowContext(ctx, "SELECT COUNT(*) FROM native_request_command").Scan(&links); err != nil {
		t.Fatal(err)
	}
	if requests != 1 || links != 1 {
		t.Fatalf("partial request persisted: requests=%d links=%d", requests, links)
	}
}

func TestNativeRequestLinksRejectMissingEndpoints(t *testing.T) {
	for _, test := range []struct {
		name      string
		requestID string
		commandID string
	}{
		{name: "missing-request", requestID: "missing", commandID: "input-1"},
		{name: "missing-command", requestID: "request-1", commandID: "missing"},
	} {
		t.Run(test.name, func(t *testing.T) {
			j := openTestJournal(t)
			ctx := context.Background()
			command := &commandpb.Command{CommandId: "input-1", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}}}
			if _, _, err := j.Admit(ctx, command, ""); err != nil {
				t.Fatal(err)
			}
			if _, err := j.conn.ExecContext(ctx, "INSERT INTO native_request VALUES ('request-1', 'claude_submit')"); err != nil {
				t.Fatal(err)
			}
			if _, err := j.conn.ExecContext(ctx, "INSERT INTO native_request_command VALUES (?, ?)", test.requestID, test.commandID); err == nil {
				t.Fatal("request link accepted a missing endpoint")
			}
			if _, err := j.conn.ExecContext(ctx, "INSERT INTO native_request_command VALUES ('request-1', 'input-1')"); err != nil {
				t.Fatal(err)
			}
		})
	}
}
