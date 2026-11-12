package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	repb "github.com/buildbuddy-io/buildbuddy/proto/remote_execution"
	longrunningpb "google.golang.org/genproto/googleapis/longrunning"
	statuspb "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/anypb"
)

const waitTestID = "/instance/uploads/original/blobs/hash/123"

type executionWaitServer struct {
	repb.UnimplementedExecutionServer
	mu      sync.Mutex
	updates []*longrunningpb.Operation
	err     error
	methods []string
	name    string
	key     []string
}

func (s *executionWaitServer) WaitExecution(req *repb.WaitExecutionRequest, stream repb.Execution_WaitExecutionServer) error {
	s.mu.Lock()
	s.name = req.GetName()
	md, _ := metadata.FromIncomingContext(stream.Context())
	s.key = md.Get("x-buildbuddy-api-key")
	s.mu.Unlock()
	for _, update := range s.updates {
		if err := stream.Send(update); err != nil {
			return err
		}
	}
	return s.err
}

func waitTestOperation(t *testing.T, done bool, response *repb.ExecuteResponse) *longrunningpb.Operation {
	t.Helper()
	stage := repb.ExecutionStage_EXECUTING
	if done {
		stage = repb.ExecutionStage_COMPLETED
	}
	md, err := anypb.New(&repb.ExecuteOperationMetadata{Stage: stage})
	if err != nil {
		t.Fatal(err)
	}
	op := &longrunningpb.Operation{Name: waitTestID, Metadata: md, Done: done}
	if response != nil {
		packed, err := anypb.New(response)
		if err != nil {
			t.Fatal(err)
		}
		op.Result = &longrunningpb.Operation_Response{Response: packed}
	}
	return op
}

func TestExecutionWaitObservesOnlyOriginalAction(t *testing.T) {
	for _, test := range []struct {
		name     string
		response *repb.ExecuteResponse
		wantErr  string
	}{
		{"omitted_defaults", &repb.ExecuteResponse{Result: &repb.ActionResult{}}, ""},
		{"explicit_ok", &repb.ExecuteResponse{Status: &statuspb.Status{}, Result: &repb.ActionResult{}}, ""},
		{"provider_error", &repb.ExecuteResponse{Status: &statuspb.Status{Code: 13, Message: "stream closed with task still claimed"}}, "status=13"},
		{"missing_result", &repb.ExecuteResponse{}, "no action result"},
		{"process_failure", &repb.ExecuteResponse{Result: &repb.ActionResult{ExitCode: 7}}, "code 7"},
	} {
		t.Run(test.name, func(t *testing.T) {
			s := &executionWaitServer{updates: []*longrunningpb.Operation{
				waitTestOperation(t, false, nil), waitTestOperation(t, true, test.response),
			}}
			updates, err := runExecutionWaitTest(t, s)
			if test.wantErr == "" && err != nil || test.wantErr != "" && (err == nil || !strings.Contains(err.Error(), test.wantErr)) {
				t.Fatalf("error = %v, want %q", err, test.wantErr)
			}
			if len(updates) != 2 || updates[0].Done || updates[0].StatusCode != nil || updates[0].ExitCode != nil || updates[0].ResultPresent {
				t.Fatalf("pending update inferred a result: %+v", updates)
			}
			last := updates[1]
			if !last.Done || last.StatusCode == nil || *last.StatusCode != test.response.GetStatus().GetCode() || last.ResultPresent != (test.response.GetResult() != nil) {
				t.Fatalf("terminal update = %+v", last)
			}
			if last.ResultPresent && (last.ExitCode == nil || *last.ExitCode != test.response.GetResult().GetExitCode()) {
				t.Fatalf("terminal exit code = %+v", last)
			}
			b, _ := json.Marshal(last)
			if test.wantErr == "" && (!strings.Contains(string(b), `"status_code":0`) || !strings.Contains(string(b), `"exit_code":0`)) {
				t.Fatalf("zero values omitted: %s", b)
			}
		})
	}
}

func TestExecutionWaitRejectsUnknownOutcomes(t *testing.T) {
	mismatch := waitTestOperation(t, true, &repb.ExecuteResponse{Result: &repb.ActionResult{}})
	mismatch.Name = strings.TrimPrefix(waitTestID, "/")
	operationError := waitTestOperation(t, true, nil)
	operationError.Metadata = nil // A terminal Operation.error need not carry metadata.
	operationError.Result = &longrunningpb.Operation_Error{Error: &statuspb.Status{Code: 5, Message: "unknown operation"}}
	missingMetadata := waitTestOperation(t, false, nil)
	missingMetadata.Metadata = nil
	for _, test := range []struct {
		name    string
		updates []*longrunningpb.Operation
		err     error
		wantErr string
	}{
		{"wrong_id", []*longrunningpb.Operation{mismatch}, nil, "identity mismatch"},
		{"unavailable", nil, status.Error(codes.NotFound, "not retained"), "not retained"},
		{"premature_eof", []*longrunningpb.Operation{waitTestOperation(t, false, nil)}, nil, "without a terminal"},
		{"transport_error", []*longrunningpb.Operation{waitTestOperation(t, false, nil)}, status.Error(codes.Unavailable, "observer disconnected"), "observer disconnected"},
		{"terminal_missing_response", []*longrunningpb.Operation{waitTestOperation(t, true, nil)}, nil, "no ExecuteResponse"},
		{"operation_error", []*longrunningpb.Operation{operationError}, nil, "status=5"},
		{"pending_missing_metadata", []*longrunningpb.Operation{missingMetadata}, nil, "no metadata"},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, err := runExecutionWaitTest(t, &executionWaitServer{updates: test.updates, err: test.err})
			if err == nil || !strings.Contains(err.Error(), test.wantErr) {
				t.Fatalf("error = %v, want %q", err, test.wantErr)
			}
		})
	}
	updates, err := runExecutionWaitTest(t, &executionWaitServer{updates: []*longrunningpb.Operation{operationError}})
	if err == nil || len(updates) != 1 || updates[0].Stage != "UNKNOWN" || updates[0].StatusCode == nil || *updates[0].StatusCode != 5 || updates[0].StatusMessage != "unknown operation" {
		t.Fatalf("terminal error without metadata: updates=%+v error=%v", updates, err)
	}
}

func TestExecutionWaitCommandCredentialsAndOutput(t *testing.T) {
	s := &executionWaitServer{updates: []*longrunningpb.Operation{waitTestOperation(t, true, &repb.ExecuteResponse{Result: &repb.ActionResult{}})}}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	repb.RegisterExecutionServer(server, s)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	t.Setenv("BUILDBUDDY_API_KEY", "test-secret-key")
	previousJSON, previousTimeout := jsonOutput, requestTimeout
	jsonOutput, requestTimeout = true, 5*time.Second
	t.Cleanup(func() { jsonOutput, requestTimeout = previousJSON, previousTimeout })
	var output bytes.Buffer
	cmd := executionWaitCmd()
	cmd.SetOut(&output)
	cmd.SetArgs([]string{"--target", "http://" + listener.Addr().String(), waitTestID})
	if err := cmd.Execute(); err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.name != waitTestID || !reflect.DeepEqual(s.key, []string{"test-secret-key"}) {
		t.Fatal("command did not preserve operation identity and API metadata")
	}
	if strings.Contains(output.String(), "test-secret-key") {
		t.Fatal("credential leaked to output")
	}
	var update executionWaitUpdate
	if err := json.Unmarshal(output.Bytes(), &update); err != nil {
		t.Fatal(err)
	}
	if !update.Done || !update.ResultPresent || update.StatusCode == nil || *update.StatusCode != 0 || update.ExitCode == nil || *update.ExitCode != 0 {
		t.Fatalf("command output = %s", output.String())
	}
}

func runExecutionWaitTest(t *testing.T, s *executionWaitServer, emitOverride ...func(executionWaitUpdate) error) ([]executionWaitUpdate, error) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.StreamInterceptor(func(srv any, stream grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
		s.mu.Lock()
		s.methods = append(s.methods, info.FullMethod)
		s.mu.Unlock()
		return handler(srv, stream)
	}), grpc.UnaryInterceptor(func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		s.mu.Lock()
		s.methods = append(s.methods, info.FullMethod)
		s.mu.Unlock()
		return handler(ctx, req)
	}))
	repb.RegisterExecutionServer(server, s)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	conn, err := newRPCConnection("http://"+listener.Addr().String(), "test-key", time.Second, 3)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ctx = metadata.AppendToOutgoingContext(ctx, "x-buildbuddy-api-key", "test-key")
	var updates []executionWaitUpdate
	err = waitForExecution(ctx, repb.NewExecutionClient(conn), waitTestID, func(update executionWaitUpdate) error {
		updates = append(updates, update)
		if len(emitOverride) != 0 {
			return emitOverride[0](update)
		}
		return nil
	})
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.name != waitTestID || !reflect.DeepEqual(s.key, []string{"test-key"}) {
		t.Fatalf("request identity=%q credentials present=%t", s.name, len(s.key) == 1)
	}
	if !reflect.DeepEqual(s.methods, []string{"/build.bazel.remote.execution.v2.Execution/WaitExecution"}) {
		t.Fatalf("unexpected RPCs (must never Execute, Cancel or retry): %v", s.methods)
	}
	return updates, err
}

func TestExecutionWaitPropagatesOutputFailure(t *testing.T) {
	s := &executionWaitServer{updates: []*longrunningpb.Operation{waitTestOperation(t, false, nil)}}
	_, err := runExecutionWaitTest(t, s, func(executionWaitUpdate) error { return io.ErrClosedPipe })
	if !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("output failure = %v", err)
	}
}
