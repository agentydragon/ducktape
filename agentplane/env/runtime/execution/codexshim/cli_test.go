package codexshim

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"github.com/bazelbuild/rules_go/go/runfiles"
)

var codexRlocation string

func TestPinnedCodexHarness(t *testing.T) {
	core := native.New()
	defer core.Close()
	var starts atomic.Int32
	server := httptest.NewServer(Handler{Server: core, observe: func(method string) {
		if method == "process/start" {
			starts.Add(1)
		}
	}})
	defer server.Close()
	dir := t.TempDir()
	home := filepath.Join(dir, "codex-home")
	skill := filepath.Join(home, "skills", "probe")
	if err := os.MkdirAll(skill, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(skill, "SKILL.md"), []byte("---\nname: shim-probe\ndescription: A probe skill for testing.\n---\nUse this skill.\n"), 0644); err != nil {
		t.Fatal(err)
	}
	binary, err := runfiles.Rlocation(codexRlocation)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	defer cancel()
	var modelCalls atomic.Int32
	modelOutput := make(chan string, 2)
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		n := modelCalls.Add(1)
		if n > 1 {
			modelOutput <- string(body)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		envelope := map[string]any{"id": fmt.Sprintf("resp_%d", n), "object": "response", "created_at": 0, "model": "gpt-5.1"}
		item := map[string]any{"id": fmt.Sprintf("item_%d", n), "status": "completed"}
		if n == 1 {
			item["type"] = "function_call"
			item["call_id"] = "call_1"
			item["name"] = "exec_command"
			item["arguments"] = `{"cmd":"printf CLI_EXEC_OK"}`
		} else {
			item["type"] = "message"
			item["role"] = "assistant"
			item["content"] = []any{map[string]any{"type": "output_text", "text": "done", "annotations": []any{}}}
		}
		emit := func(v map[string]any) { b, _ := json.Marshal(v); fmt.Fprintf(w, "data: %s\n\n", b) }
		start := map[string]any{}
		for k, v := range envelope {
			start[k] = v
		}
		start["status"] = "in_progress"
		start["output"] = []any{}
		emit(map[string]any{"type": "response.created", "response": start})
		emit(map[string]any{"type": "response.output_item.done", "output_index": 0, "item": item})
		end := map[string]any{}
		for k, v := range envelope {
			end[k] = v
		}
		end["status"] = "completed"
		end["output"] = []any{item}
		end["usage"] = map[string]any{"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "input_tokens_details": map[string]any{"cached_tokens": 0}, "output_tokens_details": map[string]any{"reasoning_tokens": 0}}
		emit(map[string]any{"type": "response.completed", "response": end})
	}))
	defer model.Close()
	cmd := exec.CommandContext(ctx, binary, "-c", `model_provider="test"`, "-c", `model_providers.test.name="test"`, "-c", fmt.Sprintf("model_providers.test.base_url=%q", model.URL+"/v1"), "-c", `model_providers.test.env_key="OPENAI_API_KEY"`, "-c", `model_providers.test.wire_api="responses"`, "-c", `approval_policy="never"`, "-c", `sandbox_mode="danger-full-access"`, "app-server")
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "HOME="+dir, "CODEX_HOME="+home, "CODEX_EXEC_SERVER_URL=ws"+strings.TrimPrefix(server.URL, "http"), "OPENAI_API_KEY=test")
	input, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	out, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	cmd.Stderr = io.Discard
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { cancel(); _ = cmd.Wait() }()
	lines := make(chan []byte, 128)
	go func() {
		defer close(lines)
		scanner := bufio.NewScanner(out)
		scanner.Buffer(make([]byte, 4096), 4<<20)
		for scanner.Scan() {
			lines <- append([]byte(nil), scanner.Bytes()...)
		}
	}()
	send := func(v any) {
		t.Helper()
		b, e := json.Marshal(v)
		if e != nil {
			t.Fatal(e)
		}
		if _, e = fmt.Fprintf(input, "%s\n", b); e != nil {
			t.Fatal(e)
		}
	}
	response := func(id float64) map[string]any {
		t.Helper()
		for {
			select {
			case b, ok := <-lines:
				if !ok {
					t.Fatal("app-server exited")
				}
				var v map[string]any
				if e := json.Unmarshal(b, &v); e != nil {
					t.Fatalf("bad frame %q: %v", b, e)
				}
				if v["id"] == id {
					if v["error"] != nil {
						t.Fatalf("rpc failed: %s", b)
					}
					return v
				}
			case <-ctx.Done():
				t.Fatalf("timed out waiting for %v", id)
			}
		}
	}
	send(map[string]any{"id": 1, "method": "initialize", "params": map[string]any{"clientInfo": map[string]any{"name": "shim-test", "version": "1"}, "capabilities": map[string]any{"experimentalApi": true}}})
	response(1)
	send(map[string]any{"method": "initialized", "params": map[string]any{}})
	send(map[string]any{"id": 2, "method": "skills/list", "params": map[string]any{"cwds": []string{dir}, "forceReload": true}})
	skills := response(2)
	encoded, _ := json.Marshal(skills)
	if !bytes.Contains(encoded, []byte("shim-probe")) {
		t.Fatalf("skill missing from response: %s", encoded)
	}
	send(map[string]any{"id": 3, "method": "thread/start", "params": map[string]any{"cwd": dir, "model": "gpt-5.1", "approvalPolicy": "never", "sandbox": "danger-full-access"}})
	thread := response(3)["result"].(map[string]any)["thread"].(map[string]any)["id"].(string)
	send(map[string]any{"id": 4, "method": "turn/start", "params": map[string]any{"threadId": thread, "input": []any{map[string]any{"type": "text", "text": "Run a shell command."}}}})
	response(4)
	select {
	case body := <-modelOutput:
		if starts.Load() == 0 {
			t.Fatal("Codex did not start a process through the shim")
		}
		if !strings.Contains(body, "CLI_EXEC_OK") {
			t.Fatalf("missing shell output in model request: %s", body)
		}
	case <-ctx.Done():
		t.Fatal("no shell result reached model")
	}
}
