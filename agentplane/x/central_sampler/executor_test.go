package centralsampler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"testing"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestExecutorRejectsMalformedInputBeforeDispatch(t *testing.T) {
	for _, tc := range []struct {
		name string
		tool string
		args string
	}{
		{"negative exec yield", "exec_command", `{"cmd":"touch /tmp/should-not-run","yield_time_ms":-1}`},
		{"missing cmd", "exec_command", `{"workdir":"/tmp"}`},
		{"non-object exec input", "exec_command", `"touch /tmp/should-not-run"`},
		{"shell the executor does not report", "exec_command", `{"cmd":"touch /tmp/should-not-run","shell":"zsh"}`},
		{"string session_id", "write_stdin", `{"session_id":"test-process"}`},
		{"fractional stdin yield", "write_stdin", `{"session_id":1000,"yield_time_ms":1.5}`},
		{"non-string chars", "write_stdin", `{"session_id":1000,"chars":true}`},
		{"missing session_id", "write_stdin", `{"chars":"x"}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// No store or transport: invalid inputs must fail before accessing either, and are
			// reported to the calling JavaScript rather than failing the Session.
			executor := &Executor{shell: &execShell{Name: "bash", Path: "/bin/bash"}}
			_, err := executor.Invoke(context.Background(), tc.tool, json.RawMessage(tc.args))
			if !errors.As(err, new(*toolError)) {
				t.Fatalf("malformed input error=%v, want a tool error", err)
			}
		})
	}
}

func TestExecutorReadRejectsMissingOutput(t *testing.T) {
	for _, raw := range []string{
		`{}`, `{"chunks":null}`, `{"chunks":[null]}`, `{"chunks":[{}]}`,
		`{"chunks":[{"chunk":null}]}`, `{"chunks":[{"chunk":"invalid base64"}]}`,
	} {
		var result executorReadResult
		if err := json.Unmarshal([]byte(raw), &result); err == nil {
			t.Errorf("accepted malformed process/read result: %s", raw)
		}
	}
	// Pinned exec-server returns an empty array when there is no output yet.
	var result executorReadResult
	if err := json.Unmarshal([]byte(`{"chunks":[],"nextSeq":0,"exited":false,"exitCode":null,"closed":false,"failure":null}`), &result); err != nil {
		t.Fatal(err)
	}
}

type recoverySocket struct {
	send    func([]byte) error
	receive func() ([]byte, error)
}

func (s *recoverySocket) Send(_ context.Context, b []byte) error  { return s.send(b) }
func (s *recoverySocket) Receive(context.Context) ([]byte, error) { return s.receive() }
func (*recoverySocket) Close(context.Context) error               { return nil }

func TestExecutorResumesOnlyObservationAfterTransportLoss(t *testing.T) {
	for _, method := range []string{"process/read", "process/start", "process/write"} {
		t.Run(method, func(t *testing.T) {
			store := newTestStore(t)
			session := openSessionForStore(t, store, "")
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
				t.Fatal(err)
			}
			var requests []executorRequest[json.RawMessage]
			var reply []byte
			broken := &recoverySocket{send: func(b []byte) error {
				var request executorRequest[json.RawMessage]
				if err := json.Unmarshal(b, &request); err != nil {
					t.Fatal(err)
				}
				requests = append(requests, request)
				return io.EOF
			}}
			replacement := &recoverySocket{send: func(b []byte) error {
				var request executorRequest[json.RawMessage]
				if err := json.Unmarshal(b, &request); err != nil {
					t.Fatal(err)
				}
				requests = append(requests, request)
				switch request.Method {
				case "initialize":
					var params executorInitialize
					if err := json.Unmarshal(request.Params, &params); err != nil {
						t.Fatal(err)
					}
					if params.ResumeSessionID == nil || *params.ResumeSessionID != "retained-executor" {
						t.Fatalf("resume=%+v", params)
					}
					reply, _ = json.Marshal(map[string]any{"id": request.ID, "result": map[string]any{"sessionId": "retained-executor"}})
				case "initialized":
				case "process/read":
					reply, _ = json.Marshal(map[string]any{"id": request.ID, "result": map[string]any{"chunks": []any{}, "nextSeq": 8, "exited": true, "exitCode": 0}})
				default:
					t.Fatalf("unexpected method %s", request.Method)
				}
				return nil
			}, receive: func() ([]byte, error) { return reply, nil }}
			opened, closed := 0, 0
			e := &Executor{
				store: store, owner: owner, socket: broken, lifetime: t.Context(), closeSocket: func() error { closed++; return nil },
				openSocket: func(context.Context, context.Context) (executorSocket, func() error, error) {
					opened++
					return replacement, func() error { closed++; return nil }, nil
				},
			}
			cursor := uint64(7)
			_, err = e.rpc(t.Context(), method, executorRead{ProcessID: "existing-process", AfterSeq: &cursor, MaxBytes: 65536}, nil)
			if method != "process/read" {
				if !errors.Is(err, io.EOF) || opened != 0 || len(requests) != 1 {
					t.Fatalf("mutation replayed: err=%v opened=%d requests=%+v", err, opened, requests)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if opened != 1 || closed != 1 || len(requests) != 4 {
				t.Fatalf("opened=%d closed=%d requests=%+v", opened, closed, requests)
			}
			if requests[0].ID != requests[3].ID || !bytes.Equal(requests[0].Params, requests[3].Params) {
				t.Fatalf("recovery changed operation: %+v", requests)
			}
			var unresolved int
			if err := store.DB().QueryRowContext(t.Context(), "SELECT count(*) FROM sampler_intent WHERE conversation_id=$1 AND result IS NULL", owner.ConversationID).Scan(&unresolved); err != nil {
				t.Fatal(err)
			}
			if unresolved != 0 {
				t.Fatalf("unresolved=%d", unresolved)
			}
			if err := e.close(); err != nil {
				t.Fatal(err)
			}
			if closed != 2 {
				t.Fatalf("replacement not closed: %d", closed)
			}
		})
	}
}

// gRPC clients may report only EOF from Send; the peer's actual status is in Recv.
type eofExecutorStream struct {
	grpc.BidiStreamingClient[executorpb.ExecutorFrame, executorpb.ExecutorFrame]
	receives int
}

func (*eofExecutorStream) Send(*executorpb.ExecutorFrame) error { return io.EOF }
func (s *eofExecutorStream) Recv() (*executorpb.ExecutorFrame, error) {
	s.receives++
	if s.receives == 1 {
		return &executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Message{Message: &executorpb.ExecutorMessage{Opcode: executorpb.ExecutorMessage_OPCODE_TEXT, Payload: []byte("{}")}}}, nil
	}
	return nil, status.Error(codes.ResourceExhausted, "executor relay queue full")
}

func TestExecutorSendEOFPreservesTerminalStatus(t *testing.T) {
	stream := &eofExecutorStream{}
	socket := gatewayExecutorSocket{stream: stream, cancel: func() {}}
	err := socket.Send(t.Context(), []byte("{}"))
	if status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("send error=%v, want terminal ResourceExhausted", err)
	}
}

func TestExecutorReadRecoveryDoesNotReplaceMissingSession(t *testing.T) {
	for _, result := range []string{
		`{"error":{"code":-32600,"message":"unknown session id retained-executor"}}`,
		`{"result":{"sessionId":"new-executor"}}`,
	} {
		t.Run(result, func(t *testing.T) {
			store := newTestStore(t)
			session := openSessionForStore(t, store, "")
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
				t.Fatal(err)
			}
			var reply []byte
			opens, reads := 0, 0
			broken := &recoverySocket{send: func([]byte) error { return io.EOF }}
			replacement := &recoverySocket{send: func(b []byte) error {
				var request executorRequest[json.RawMessage]
				if err := json.Unmarshal(b, &request); err != nil {
					t.Fatal(err)
				}
				if request.Method == "process/read" {
					reads++
				}
				var response map[string]any
				if err := json.Unmarshal([]byte(result), &response); err != nil {
					t.Fatal(err)
				}
				response["id"] = request.ID
				reply, _ = json.Marshal(response)
				return nil
			}, receive: func() ([]byte, error) { return reply, nil }}
			e := &Executor{
				store: store, owner: owner, socket: broken, lifetime: t.Context(), closeSocket: func() error { return nil },
				openSocket: func(context.Context, context.Context) (executorSocket, func() error, error) {
					opens++
					return replacement, func() error { return nil }, nil
				},
			}
			_, err = e.rpc(t.Context(), "process/read", executorRead{ProcessID: "existing"}, nil)
			if err == nil || opens != 1 || reads != 0 {
				t.Fatalf("unsafe session substitution: err=%v opens=%d reads=%d", err, opens, reads)
			}
			conversation, err := store.Read(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			if conversation.ExecutorSession == nil || *conversation.ExecutorSession != "retained-executor" {
				t.Fatalf("retained session changed: %+v", conversation.ExecutorSession)
			}
		})
	}
}
