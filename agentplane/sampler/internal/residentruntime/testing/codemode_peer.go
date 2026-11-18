package testing

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/jsonrpc"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/codemode"
)

type CodeModePeer struct {
	input     io.ReadCloser
	output    io.WriteCloser
	writeMu   sync.Mutex
	closeOnce sync.Once
	Frames    chan map[string]json.RawMessage
	closed    chan struct{}
	// acknowledge lists the offered optional capabilities this host supports.
	acknowledge []string
}

func newCodeModePeer(connection net.Conn, acknowledge []string) *CodeModePeer {
	peer := &CodeModePeer{input: connection, output: connection, Frames: make(chan map[string]json.RawMessage, 32), closed: make(chan struct{}), acknowledge: acknowledge}
	return peer
}

func (p *CodeModePeer) serve() {
	payload, err := readHostFrame(p.input)
	if err != nil {
		return
	}
	var hello codemode.Hello
	if jsonrpc.UnmarshalObject(payload, &hello) != nil || hello.Type != "connection/hello" {
		return
	}
	granted := []string{}
	for _, capability := range hello.OptionalCapabilities {
		if slices.Contains(p.acknowledge, capability) {
			granted = append(granted, capability)
		}
	}
	_ = p.Send(codemode.Frame{Type: codemode.Ready, SelectedVersion: 1, Capabilities: granted})
	for {
		payload, err = readHostFrame(p.input)
		if err != nil {
			return
		}
		var fields map[string]json.RawMessage
		if jsonrpc.UnmarshalObject(payload, &fields) != nil {
			return
		}
		select {
		case p.Frames <- fields:
		default:
		}
		var frame struct {
			Type    string          `json:"type"`
			ID      int64           `json:"id"`
			Request json.RawMessage `json:"request"`
		}
		if jsonrpc.UnmarshalObject(payload, &frame) != nil || frame.Type != "operation/request" {
			continue
		}
		var request struct {
			Method    string `json:"method"`
			SessionID string `json:"sessionId"`
		}
		if jsonrpc.UnmarshalObject(frame.Request, &request) != nil || request.Method != "session/open" {
			continue
		}
		value, _ := json.Marshal(codemode.HostResponse{Type: "session/ready", SessionID: request.SessionID})
		_ = p.Send(codemode.Frame{Type: codemode.Response, ID: &frame.ID, Result: &codemode.WireResult{Status: "ok", Value: value}})
	}
}

func (p *CodeModePeer) Send(value any) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	p.writeMu.Lock()
	defer p.writeMu.Unlock()
	return writeHostFrame(p.output, payload)
}

func (p *CodeModePeer) SendDelegate(id int64, sessionID string) error {
	return p.SendNotification(id, sessionID, "cell-1", " ")
}

func (p *CodeModePeer) SendNotification(id int64, sessionID, cellID, text string) error {
	return p.Send(codemode.Frame{
		Type: codemode.DelegateRequest, ID: &id, SessionID: sessionID,
		Request: &codemode.Delegate{Type: "notification/send", CallID: "exec-call", CellID: cellID, Text: text},
	})
}

func (p *CodeModePeer) Crash() {
	_ = p.output.Close()
}

func (p *CodeModePeer) Close() error {
	p.closeOnce.Do(func() {
		_ = p.input.Close()
		_ = p.output.Close()
		close(p.closed)
	})
	return nil
}

func NewCodeModeManager(t *testing.T, rpcTimeout, observationTimeout time.Duration, acknowledge ...string) (*codemode.Manager, func() []*CodeModePeer, string) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	directory := t.TempDir()
	binary := filepath.Join(directory, "host")
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	script := fmt.Sprintf("#!/bin/sh\nCODEMODE_TEST_PEER=%s exec %q \"$@\"\n", listener.Addr(), executable)
	if err := os.WriteFile(binary, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	manager := codemode.NewManager(context.Background(), binary, directory, rpcTimeout, observationTimeout)
	var mu sync.Mutex
	var peers []*CodeModePeer
	go func() {
		for {
			connection, err := listener.Accept()
			if err != nil {
				return
			}
			peer := newCodeModePeer(connection, acknowledge)
			mu.Lock()
			peers = append(peers, peer)
			mu.Unlock()
			go peer.serve()
		}
	}()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = manager.Close(ctx)
		if t.Failed() {
			RetainFailedCodeModeHostLog(t, directory)
		}
		mu.Lock()
		defer mu.Unlock()
		for _, peer := range peers {
			_ = peer.Close()
		}
	})
	return manager, func() []*CodeModePeer {
		mu.Lock()
		defer mu.Unlock()
		return append([]*CodeModePeer(nil), peers...)
	}, directory
}

func RetainFailedCodeModeHostLog(t *testing.T, directory string) {
	t.Helper()
	outputs := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if outputs == "" {
		return
	}
	logPath := filepath.Join(directory, "code-mode-host.log")
	log, err := os.ReadFile(logPath)
	if err != nil {
		t.Logf("read failed Code Mode host stderr log: %v", err)
		return
	}
	if err := os.MkdirAll(outputs, 0o750); err != nil {
		t.Logf("create undeclared test output directory: %v", err)
		return
	}
	testName := strings.NewReplacer("/", "_", " ", "_").Replace(t.Name())
	name := testName + "-" + filepath.Base(directory) + "-code-mode-host.log"
	if err := os.WriteFile(filepath.Join(outputs, name), log, 0o640); err != nil {
		t.Logf("write failed Code Mode host stderr log: %v", err)
	}
}

type CodeOperationRequest struct {
	Request   json.RawMessage
	ID        int64
	Method    string
	SessionID string
}

func ReceiveCodeOperationRequest(t *testing.T, peer *CodeModePeer, sessionID, method string) CodeOperationRequest {
	t.Helper()
	deadline := time.After(3 * time.Second)
	for {
		select {
		case fields := <-peer.Frames:
			var envelope struct {
				Type    string          `json:"type"`
				ID      int64           `json:"id"`
				Request json.RawMessage `json:"request"`
			}
			if err := jsonrpc.UnmarshalObject(MarshalFields(t, fields), &envelope); err != nil {
				t.Fatal(err)
			}
			if envelope.Type != "operation/request" {
				continue
			}
			var request struct {
				Method    string `json:"method"`
				SessionID string `json:"sessionId"`
			}
			if err := jsonrpc.UnmarshalObject(envelope.Request, &request); err != nil {
				t.Fatal(err)
			}
			if request.Method == method && (sessionID == "" || request.SessionID == sessionID) {
				return CodeOperationRequest{ID: envelope.ID, Method: request.Method, SessionID: request.SessionID, Request: envelope.Request}
			}
		case <-deadline:
			t.Fatalf("session %q did not issue %s", sessionID, method)
			return CodeOperationRequest{}
		}
	}
}

func MarshalFields(t *testing.T, fields map[string]json.RawMessage) []byte {
	t.Helper()
	data, err := json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func RunCodeModePeer() bool {
	address := os.Getenv("CODEMODE_TEST_PEER")
	if address == "" {
		return false
	}
	connection, err := net.Dial("tcp", address)
	if err != nil {
		panic(err)
	}
	defer connection.Close()
	go func() { _, _ = io.Copy(connection, os.Stdin) }()
	_, _ = io.Copy(os.Stdout, connection)
	return true
}

func readHostFrame(reader io.Reader) ([]byte, error) {
	var size uint32
	if err := binary.Read(reader, binary.LittleEndian, &size); err != nil {
		return nil, err
	}
	data := make([]byte, int(size))
	_, err := io.ReadFull(reader, data)
	return data, err
}

func writeHostFrame(writer io.Writer, payload []byte) error {
	if err := binary.Write(writer, binary.LittleEndian, uint32(len(payload))); err != nil {
		return err
	}
	_, err := io.Copy(writer, strings.NewReader(string(payload)))
	return err
}

func AssertDelegateResponse(t *testing.T, fields map[string]json.RawMessage, wantID int64, wantStatus string) {
	t.Helper()
	var response codemode.DelegateResponse
	if err := jsonrpc.UnmarshalObject(MarshalFields(t, fields), &response); err != nil {
		t.Fatal(err)
	}
	if response.Type != "delegate/response" || response.ID != wantID || response.Result.Status != wantStatus {
		t.Fatalf("delegate response=%+v, want id=%d status=%q", response, wantID, wantStatus)
	}
}
