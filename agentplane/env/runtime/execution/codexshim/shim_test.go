package codexshim

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestUnsupportedSandboxIsRejected(t *testing.T) {
	ctx := context.Background()
	ws := dialShim(t, ctx, serveShim(t, DefaultLimits()))
	if v := shimCall(t, ctx, ws, 1, "initialize", map[string]any{"clientName": "test"}); v["result"] == nil {
		t.Fatalf("initialize: %v", v)
	}
	v := shimCall(t, ctx, ws, 2, "process/start", map[string]any{"processId": "p", "argv": []string{"/bin/true"}, "cwd": "file:///tmp", "env": map[string]string{}, "sandbox": map[string]any{"type": "workspaceWrite"}})
	if v["error"] == nil {
		t.Fatalf("sandbox request accepted: %v", v)
	}
}

func TestUnknownResumeUsesNativeRejection(t *testing.T) {
	ctx := context.Background()
	ws := dialShim(t, ctx, serveShim(t, DefaultLimits()))
	v := shimCall(t, ctx, ws, 1, "initialize", map[string]any{"clientName": "test", "resumeSessionId": "missing"})
	if e, _ := v["error"].(map[string]any); e["code"] != float64(-32600) || e["message"] != "unknown session id missing" {
		t.Fatalf("unexpected response: %v", v)
	}
}

func TestFilesystemRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := (&url.URL{Scheme: "file", Path: filepath.Join(dir, "file")}).String()
	if _, err := filesystem("fs/writeFile", params{Path: path, DataBase64: base64.StdEncoding.EncodeToString([]byte("hello"))}); err != nil {
		t.Fatal(err)
	}
	got, err := filesystem("fs/readFile", params{Path: path})
	if err != nil {
		t.Fatal(err)
	}
	if got.(map[string]any)["dataBase64"] != base64.StdEncoding.EncodeToString([]byte("hello")) {
		t.Fatalf("unexpected read: %v", got)
	}
	no := false
	if _, err := filesystem("fs/readFile", params{Path: path, FollowSymlinks: &no}); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	if _, err := filesystem("fs/readFile", params{Path: uri(link), FollowSymlinks: &no}); err == nil {
		t.Fatal("followed symlink despite no-follow")
	}
}

func TestProcessNotifications(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	ws := dialShim(t, ctx, serveShim(t, DefaultLimits()))
	shimCall(t, ctx, ws, 1, "initialize", map[string]any{"clientName": "test"})
	if v := shimCall(t, ctx, ws, 2, "process/start", map[string]any{"processId": "p", "argv": []string{"/bin/sh", "-c", "printf hello"}, "cwd": "file:///tmp", "env": map[string]string{}}); v["error"] != nil {
		t.Fatalf("request failed: %v", v)
	}
	seen := map[string]bool{}
	for !seen["process/closed"] {
		msg := readShim(t, ctx, ws)
		if msg["error"] != nil {
			t.Fatalf("request failed: %v", msg)
		}
		method, _ := msg["method"].(string)
		seen[method] = true
	}
	if !seen["process/output"] || !seen["process/exited"] {
		t.Fatalf("missing notifications: %v", seen)
	}
}

func TestDiscoveryAndCopy(t *testing.T) {
	root := t.TempDir()
	skill := filepath.Join(root, "skills", "sample")
	if err := os.MkdirAll(skill, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(skill, "SKILL.md"), []byte("instructions"), 0644); err != nil {
		t.Fatal(err)
	}
	var p params
	p.Roots = append(p.Roots, struct {
		ID      string          `json:"id"`
		Path    string          `json:"path"`
		Sandbox json.RawMessage `json:"sandbox"`
	}{ID: "root", Path: uri(root)})
	result, err := discover(p)
	if err != nil {
		t.Fatal(err)
	}
	roots := result.(map[string]any)["roots"].([]rootResult)
	if len(roots) != 1 || roots[0].Error != nil || len(roots[0].Skills) != 1 || roots[0].Skills[0].Instructions.Contents != "instructions" {
		t.Fatalf("bad discovery: %#v", roots)
	}
	destination := filepath.Join(t.TempDir(), "copy")
	yes := true
	if _, err := copyRequest(params{SourcePath: uri(root), DestinationPath: uri(destination), Recursive: &yes}); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(filepath.Join(destination, "skills", "sample", "SKILL.md"))
	if err != nil || string(b) != "instructions" {
		t.Fatalf("copy=%q error=%v", b, err)
	}
	if _, err := copyRequest(params{SourcePath: uri(root), DestinationPath: uri(filepath.Join(root, "child")), Recursive: &yes}); err == nil {
		t.Fatal("allowed copying into source")
	}
}

func TestFileHandlesDoNotSurviveReconnect(t *testing.T) {
	wsURL := serveShim(t, DefaultLimits())
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	ws := dialShim(t, ctx, wsURL)
	init := shimCall(t, ctx, ws, 1, "initialize", map[string]any{"clientName": "test"})
	id := init["result"].(map[string]any)["sessionId"].(string)
	path := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(path, []byte("hello"), 0644); err != nil {
		t.Fatal(err)
	}
	opened := shimCall(t, ctx, ws, 2, "fs/open", map[string]any{"handleId": "file", "path": uri(path)})
	if opened["error"] != nil {
		t.Fatalf("open: %v", opened)
	}
	ws.CloseNow()
	// Detach is asynchronous; retry only the handshake, which has no side effects when attached.
	var resumed *websocket.Conn
	for ctx.Err() == nil {
		candidate := dialShim(t, ctx, wsURL)
		v := shimCall(t, ctx, candidate, 3, "initialize", map[string]any{"clientName": "test", "resumeSessionId": id})
		if v["error"] == nil {
			resumed = candidate
			break
		}
		candidate.CloseNow()
		time.Sleep(time.Millisecond * 10)
	}
	if resumed == nil {
		t.Fatal("resume not available")
	}
	got := shimCall(t, ctx, resumed, 4, "fs/readBlock", map[string]any{"handleId": "file", "offset": 0, "len": 10})
	if got["error"] == nil {
		t.Fatalf("old handle remained usable: %v", got)
	}
}

func TestBufferedHTTPRequest(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Add("X-Result", "one")
		w.Header().Add("X-Result", "two")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte("reply"))
	}))
	defer server.Close()
	result, err := httpRequest(context.Background(), params{HTTPMethod: "GET", HTTPURL: server.URL})
	if err != nil {
		t.Fatal(err)
	}
	got := result.(map[string]any)
	if got["status"] != http.StatusCreated || string(got["bodyBase64"].([]byte)) != "reply" {
		t.Fatalf("response: %v", got)
	}
	if _, err := httpRequest(context.Background(), params{HTTPMethod: "GET", HTTPURL: server.URL, StreamResponse: true}); err == nil {
		t.Fatal("accepted unsupported streaming")
	}
}

func TestWalkFollowsDirectorySymlinks(t *testing.T) {
	root := t.TempDir()
	target := t.TempDir()
	if err := os.WriteFile(filepath.Join(target, "item"), []byte("x"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}
	var p params
	p.Path = uri(root)
	p.Options.MaxDepth = 3
	p.Options.MaxDirectories = 20
	p.Options.MaxEntries = 20
	p.Options.FollowDirectorySymlinks = true
	result, err := filesystem("fs/walk", p)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(result)
	if !strings.Contains(string(b), "/link/item") {
		t.Fatalf("missing symlinked file: %s", b)
	}
}

func TestWritableFileStream(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	ws := dialShim(t, ctx, serveShim(t, DefaultLimits()))
	if got := shimCall(t, ctx, ws, 1, "initialize", map[string]any{"clientName": "test"}); got["error"] != nil {
		t.Fatal(got)
	}
	path := filepath.Join(t.TempDir(), "file")
	if got := shimCall(t, ctx, ws, 2, "fs/open", map[string]any{"handleId": "f", "path": uri(path), "mode": "replace"}); got["error"] != nil {
		t.Fatal(got)
	}
	if got := shimCall(t, ctx, ws, 3, "fs/writeBlock", map[string]any{"handleId": "f", "offset": 0, "chunk": []byte("hello")}); got["error"] != nil {
		t.Fatal(got)
	}
	if got := shimCall(t, ctx, ws, 4, "fs/close", map[string]any{"handleId": "f"}); got["error"] != nil {
		t.Fatal(got)
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != "hello" {
		t.Fatalf("data=%q err=%v", data, err)
	}
}

func TestStreamedHTTPAndEnvHeader(t *testing.T) {
	t.Setenv("TEST_EXEC_HTTP_KEY", "secret-value")
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Probe") != "Bearer secret-value" {
			t.Errorf("header: %q", r.Header.Get("X-Probe"))
		}
		_, _ = w.Write([]byte("streamed"))
	}))
	defer upstream.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	ws := dialShim(t, ctx, serveShim(t, DefaultLimits()))
	if got := shimCall(t, ctx, ws, 1, "initialize", map[string]any{"clientName": "test"}); got["error"] != nil {
		t.Fatal(got)
	}
	if got := shimCall(t, ctx, ws, 2, "http/request", map[string]any{"requestId": "req", "method": "GET", "url": upstream.URL, "streamResponse": true, "headers": []any{map[string]any{"name": "X-Probe", "value": "Bearer ", "valueEnvVar": "TEST_EXEC_HTTP_KEY"}}}); got["error"] != nil || got["result"] == nil {
		t.Fatalf("headers: %v", got)
	}
	var out []byte
	for {
		got := readShim(t, ctx, ws)
		if got["method"] != "http/request/bodyDelta" {
			t.Fatalf("notification: %v", got)
		}
		p := got["params"].(map[string]any)
		chunk, err := base64.StdEncoding.DecodeString(p["deltaBase64"].(string))
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, chunk...)
		if p["done"] == true {
			break
		}
	}
	if string(out) != "streamed" {
		t.Fatalf("body: %q", out)
	}
	if got := shimCall(t, ctx, ws, 3, "http/request", map[string]any{"requestId": "blocked", "method": "GET", "url": upstream.URL, "headers": []any{map[string]any{"name": "x", "value": "", "valueEnvVar": "OPENAI_API_KEY"}}}); got["error"] == nil {
		t.Fatal("allowed denied environment variable")
	}
}

func TestNoFollowDirectoryAndRemoval(t *testing.T) {
	root := t.TempDir()
	elsewhere := t.TempDir()
	if err := os.Symlink(elsewhere, filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}
	no, yes := false, true
	if _, err := filesystem("fs/createDirectory", params{Path: uri(filepath.Join(root, "link", "child")), FollowSymlinks: &no, Recursive: &yes}); err == nil {
		t.Fatal("followed symlink creating directory")
	}
	if _, err := os.Stat(filepath.Join(elsewhere, "child")); !os.IsNotExist(err) {
		t.Fatalf("unexpected creation: %v", err)
	}
	path := filepath.Join(root, "a", "b")
	if _, err := filesystem("fs/createDirectory", params{Path: uri(path), FollowSymlinks: &no, Recursive: &yes}); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(path, "f"), []byte("x"), 0644); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{filepath.Join(path, "f"), path, filepath.Join(root, "a")} {
		if _, err := filesystem("fs/remove", params{Path: uri(path), FollowSymlinks: &no, Recursive: &no}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := os.Stat(filepath.Join(root, "a")); !os.IsNotExist(err) {
		t.Fatalf("directory remained: %v", err)
	}
}
