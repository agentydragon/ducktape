package typedbackend

import (
	"context"
	"net/url"
	"os"
	"path/filepath"
	"testing"

	pb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
)

func TestCloseFileMarksUnknownHandle(t *testing.T) {
	core := native.New()
	t.Cleanup(func() { _ = core.Close() })
	session, err := core.OpenSession("")
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "sample")
	if err := os.WriteFile(path, []byte("hello"), 0600); err != nil {
		t.Fatal(err)
	}
	server := New(core)
	ctx := context.Background()
	opened, err := server.OpenFile(ctx, &pb.OpenFileRequest{ExecutionSessionId: session.ID(), RequestId: "open", HandleId: "test-handle", Path: (&url.URL{Scheme: "file", Path: path}).String()})
	if err != nil || opened.GetStatus().GetOutcome() != pb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("open file = %v, %v", opened, err)
	}
	closeHandle := func() *pb.ExecutionStatus {
		t.Helper()
		response, err := server.CloseFile(ctx, &pb.CloseFileRequest{ExecutionSessionId: session.ID(), RequestId: "close", HandleId: "test-handle"})
		if err != nil {
			t.Fatal(err)
		}
		return response.GetStatus()
	}
	if status := closeHandle(); status.GetOutcome() != pb.ExecutionStatus_OUTCOME_COMPLETED || status.UpstreamCode != nil {
		t.Fatalf("first close = %v", status)
	}
	if status := closeHandle(); status.GetOutcome() != pb.ExecutionStatus_OUTCOME_REJECTED || status.GetUpstreamCode() != UnknownFileCode {
		t.Fatalf("second close = %v, want rejection with UnknownFileCode", status)
	}
}
