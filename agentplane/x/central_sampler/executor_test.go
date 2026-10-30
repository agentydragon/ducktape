package centralsampler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"sync"
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

func TestExecutorDrainsOutputThatArrivesAfterExit(t *testing.T) {
	for _, operation := range []string{"setup", "command", "reconcile"} {
		t.Run(operation, func(t *testing.T) {
			store := newTestStore(t)
			session := openSessionForStore(t, store, "")
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			reads := 0
			var reply []byte
			socket := &recoverySocket{
				send: func(b []byte) error {
					var request executorRequest[json.RawMessage]
					if err := json.Unmarshal(b, &request); err != nil {
						t.Fatal(err)
					}
					var result any
					switch request.Method {
					case "process/start":
						var start executorStart
						if err := json.Unmarshal(request.Params, &start); err != nil {
							t.Fatal(err)
						}
						result = executorStarted{ProcessID: start.ProcessID}
					case "process/read":
						var read executorRead
						if err := json.Unmarshal(request.Params, &read); err != nil {
							t.Fatal(err)
						}
						if reads == 0 && read.AfterSeq != nil || reads > 0 && (read.AfterSeq == nil || *read.AfterSeq != uint64(reads)) {
							t.Fatalf("read %d cursor=%v", reads, read.AfterSeq)
						}
						page := executorReadResult{Chunks: []executorChunk{}, Exited: true, ExitCode: ptrInt(23), NextSeq: uint64(reads + 2)}
						switch reads {
						case 0: // The process exited; its stdout reader has not published output yet.
						case 1:
							page.Chunks = []executorChunk{{Seq: 2, Stream: "stdout", Chunk: []byte("late stdout\n")}}
							page.Closed = true
						case 2:
							page.Closed = true
						default:
							t.Fatal("read after terminal empty page")
						}
						reads++
						result = page
					default:
						t.Fatalf("unexpected method %s", request.Method)
					}
					reply, _ = json.Marshal(map[string]any{"id": request.ID, "result": result})
					return nil
				},
				receive: func() ([]byte, error) { return reply, nil },
			}
			e := &Executor{store: store, owner: owner, socket: socket, lifetime: t.Context(), cwd: "/tmp", processLocks: map[string]*sync.Mutex{}}
			var output string
			var code int
			if operation == "setup" {
				code, err = e.Setup(t.Context(), "printf 'late stdout\\n'; exit 23", func(stream string, b []byte) error {
					if stream != "stdout" {
						t.Fatalf("unexpected stream %s", stream)
					}
					output += string(b)
					return nil
				})
			} else {
				if err := store.RegisterProcess(t.Context(), owner, "late-output", false); err != nil {
					t.Fatal(err)
				}
				if err := store.ProcessResult(t.Context(), owner, "late-output", nil, "running", nil, false); err != nil {
					t.Fatal(err)
				}
				if operation == "reconcile" {
					for range 3 {
						if _, err := e.ReconcileProcesses(t.Context(), time.Second); err != nil {
							t.Fatal(err)
						}
					}
				}
				var observed *processObservation
				observed, err = e.readProcess(t.Context(), "late-output", 1000)
				if err == nil {
					output, code = string(observed.output.retained()), *observed.exitCode
				}
			}
			if err != nil || code != 23 || output != "late stdout\n" || reads != 3 {
				t.Fatalf("exit=%d output=%q reads=%d error=%v", code, output, reads, err)
			}
		})
	}
}

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

func TestExecutorReconnectsBeforeNextOperationWithoutReplayingMutation(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterProcess(t.Context(), owner, "existing-process", false); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, "existing-process", nil, "running", nil, false); err != nil {
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
			reply, _ = json.Marshal(map[string]any{"id": request.ID, "result": map[string]any{
				"chunks": []any{}, "nextSeq": 0, "exited": false, "exitCode": nil, "closed": false, "failure": nil,
			}})
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
	_, err = e.rpc(t.Context(), "process/start", executorStart{ProcessID: "lost-start", Argv: []string{"touch", "once"}}, nil)
	var transport *executorTransportError
	if !errors.As(err, &transport) {
		t.Fatalf("start error=%v, want explicit unknown transport outcome", err)
	}
	if transport.intentID == "" || transport.processID != "lost-start" || !strings.Contains(transport.Error(), "remote effect outcome is unknown") {
		t.Fatalf("transport evidence=%+v error=%v", transport, transport)
	}
	_, err = e.rpc(t.Context(), "process/read", executorRead{ProcessID: "existing-process", MaxBytes: 65536}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if opened != 1 || closed != 1 {
		t.Fatalf("opened=%d closed=%d", opened, closed)
	}
	if len(requests) != 4 || requests[0].Method != "process/start" || requests[1].Method != "initialize" ||
		requests[2].Method != "initialized" || requests[3].Method != "process/read" {
		t.Fatalf("requests=%+v", requests)
	}
	starts := 0
	for _, request := range requests {
		if request.Method == "process/start" {
			starts++
		}
	}
	if starts != 1 {
		t.Fatalf("lost process/start was replayed: %+v", requests)
	}
	var unresolved int
	if err := store.DB().QueryRowContext(t.Context(), "SELECT count(*) FROM sampler_intent WHERE conversation_id=$1 AND result IS NULL", owner.ConversationID).Scan(&unresolved); err != nil {
		t.Fatal(err)
	}
	if unresolved != 0 {
		t.Fatalf("unresolved=%d", unresolved)
	}
}

func TestReconcileAbandonsProcessAfterTransportLoss(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	for _, processID := range []string{"live-process-1", "live-process-2"} {
		if err := store.RegisterProcess(t.Context(), owner, processID, false); err != nil {
			t.Fatal(err)
		}
		if err := store.ProcessResult(t.Context(), owner, processID, nil, "running", nil, false); err != nil {
			t.Fatal(err)
		}
		if err := store.RetainProcessOutput(t.Context(), owner, processID, 4, []byte("already observed"), false, nil); err != nil {
			t.Fatal(err)
		}
	}
	e := &Executor{
		store: store, owner: owner,
		socket:       &recoverySocket{send: func([]byte) error { return io.EOF }},
		lifetime:     t.Context(),
		processLocks: map[string]*sync.Mutex{},
	}
	for range 2 {
		progressed, err := e.ReconcileProcesses(t.Context(), time.Second)
		if err != nil || !progressed {
			t.Fatalf("progressed=%t error=%v", progressed, err)
		}
	}
	for _, processID := range []string{"live-process-1", "live-process-2"} {
		process, err := store.Process(t.Context(), owner, processID)
		if err != nil {
			t.Fatal(err)
		}
		if process.Status != "abandoned" || process.AfterSequence == nil || *process.AfterSequence != 4 ||
			string(process.BufferedOutput) != "already observed" {
			t.Fatalf("abandoned process lost evidence: %+v", process)
		}
	}
	state, err := store.ExecutionState(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if len(state.Processes) != 0 {
		t.Fatalf("abandoned process remains scheduled: %+v", state.Processes)
	}
	var result []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT result FROM sampler_intent WHERE conversation_id=$1 AND request->>'method'='process/read'`, owner.ConversationID).Scan(&result); err != nil {
		t.Fatal(err)
	}
	var evidence map[string]any
	if err := json.Unmarshal(result, &evidence); err != nil {
		t.Fatal(err)
	}
	if evidence["remote_outcome"] != "unknown" || evidence["automatic_replay"] != false {
		t.Fatalf("transport result evidence=%+v", evidence)
	}
}
