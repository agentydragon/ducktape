package codexshim

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestFilesystemRPCParity(t *testing.T) {
	limits := DefaultLimits()
	limits.MaxReadDirectoryEntries = 4
	limits.MaxReadFileBytes = 5
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	ws := dialShim(t, ctx, serveShim(t, limits))

	call := func(method string, p map[string]any, wantCode int, out any) {
		t.Helper()
		request, err := json.Marshal(map[string]any{"id": 1, "method": method, "params": p})
		if err != nil {
			t.Fatal(err)
		}
		if err := ws.Write(ctx, websocket.MessageText, request); err != nil {
			t.Fatal(err)
		}
		_, response, err := ws.Read(ctx)
		if err != nil {
			t.Fatal(err)
		}
		var reply struct {
			Result json.RawMessage `json:"result"`
			Error  *struct {
				Code int `json:"code"`
			} `json:"error"`
		}
		if err := json.Unmarshal(response, &reply); err != nil {
			t.Fatal(err)
		}
		if wantCode != 0 {
			if reply.Error == nil || reply.Error.Code != wantCode {
				t.Fatalf("%s %v: want error %d, got %s", method, p, wantCode, response)
			}
			return
		}
		if reply.Error != nil || reply.Result == nil {
			t.Fatalf("%s %v: want result, got %s", method, p, response)
		}
		if out != nil {
			if err := json.Unmarshal(reply.Result, out); err != nil {
				t.Fatal(err)
			}
		}
	}
	call("initialize", map[string]any{"clientName": "test"}, 0, nil)

	root := t.TempDir()
	file := filepath.Join(root, "file")
	if err := os.WriteFile(file, []byte("target"), 0600); err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(root, "dir")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "child"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	for name, target := range map[string]string{"file-link": "file", "dir-link": "dir", "broken": "missing"} {
		if err := os.Symlink(target, filepath.Join(root, name)); err != nil {
			t.Fatal(err)
		}
	}
	pathParams := func(name string) map[string]any {
		return map[string]any{"path": uri(filepath.Join(root, name))}
	}
	var metadata struct {
		IsDirectory bool
		IsFile      bool
		IsSymlink   bool
		Size        int64
	}
	call("fs/getMetadata", pathParams("file-link"), 0, &metadata)
	if !metadata.IsSymlink || !metadata.IsFile || metadata.IsDirectory || metadata.Size != 6 {
		t.Fatalf("metadata for file symlink: %+v", metadata)
	}
	call("fs/getMetadata", pathParams("dir-link"), 0, &metadata)
	if !metadata.IsSymlink || metadata.IsFile || !metadata.IsDirectory {
		t.Fatalf("metadata for directory symlink: %+v", metadata)
	}
	call("fs/getMetadata", pathParams("broken"), -32004, nil)
	noFollow := pathParams("file-link")
	noFollow["followSymlinks"] = false
	call("fs/getMetadata", noFollow, -32600, nil)

	type kind struct{ IsDirectory, IsFile bool }
	var directory struct {
		Entries []struct {
			FileName            string
			IsDirectory, IsFile bool
		}
	}
	call("fs/readDirectory", pathParams(""), 0, &directory)
	got := make(map[string]kind)
	for _, e := range directory.Entries {
		got[e.FileName] = kind{IsDirectory: e.IsDirectory, IsFile: e.IsFile}
	}
	want := map[string]kind{"file": {IsFile: true}, "file-link": {IsFile: true}, "dir": {IsDirectory: true}, "dir-link": {IsDirectory: true}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("fs/readDirectory entries = %v, want %v", got, want)
	}
	if err := os.WriteFile(filepath.Join(root, "fifth-entry"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	call("fs/readDirectory", pathParams(""), -32603, nil)

	call("fs/readFile", pathParams("missing"), -32004, nil)
	call("fs/readFile", pathParams("file"), -32600, nil)
	call("fs/readFile", map[string]any{"path": uri(file), "followSymlinks": false}, -32600, nil)
	call("fs/readFile", pathParams("dir"), -32600, nil)
	call("fs/readFile", map[string]any{"path": "relative"}, -32602, nil)
	call("fs/getMetadata", pathParams("file/child"), -32603, nil)
	call("fs/writeFile", map[string]any{"path": uri(file), "dataBase64": "!"}, -32600, nil)
	call("fs/writeFile", map[string]any{"path": uri(file)}, -32602, nil)
	call("fs/writeFile", map[string]any{"path": uri(file), "dataBase64": nil}, -32602, nil)
	if content, err := os.ReadFile(file); err != nil || string(content) != "target" {
		t.Fatalf("invalid writes changed file: %q, %v", content, err)
	}

	call("fs/remove", pathParams("missing"), 0, nil)
	missing := pathParams("missing")
	missing["force"] = false
	call("fs/remove", missing, -32004, nil)
	noFollow["recursive"] = false
	call("fs/remove", noFollow, -32600, nil)
	noFollow["path"] = uri(filepath.Join(root, "missing", "child"))
	call("fs/remove", noFollow, 0, nil)
	noFollow["force"] = false
	call("fs/remove", noFollow, -32004, nil)
	call("fs/remove", map[string]any{"path": uri(dir), "followSymlinks": false}, -32603, nil)
	call("fs/remove", map[string]any{"path": uri(dir), "recursive": false}, -32603, nil)
	call("fs/remove", pathParams("dir-link"), 0, nil)
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("removing a directory symlink removed its target: %v", err)
	}
	call("fs/remove", pathParams("dir"), 0, nil)
	if _, err := os.Lstat(dir); !os.IsNotExist(err) {
		t.Fatalf("default removal of nonempty directory: %v", err)
	}
}

func TestWalkLimitsIncludeSerializedErrors(t *testing.T) {
	root := t.TempDir()
	link := filepath.Join(root, "a<broken>")
	if err := os.Symlink("missing", link); err != nil {
		t.Fatal(err)
	}
	var p params
	p.Options.MaxDepth = 2
	p.Options.MaxDirectories = 1
	p.Options.MaxEntries = 2
	p.Options.FollowDirectorySymlinks = true
	p.maxWalkDepth = 1
	if _, err := walkFiles(root, p); filesystemRPCError(err) != (rpcError{-32600, "filesystem walk limits exceed maximums: depth=1, directories=10000, entries=50000"}) {
		t.Fatalf("walk validation error = %v", err)
	}
	p.maxWalkDepth = 3
	p.maxWalkResponseBytes = 512
	result, err := walkFiles(root, p)
	if err != nil {
		t.Fatal(err)
	}
	full, err := json.Marshal(result)
	if err != nil || !strings.Contains(string(full), `"message":`) {
		t.Fatalf("walk response without truncation = %s, %v", full, err)
	}
	p.maxWalkResponseBytes = len(full) - 1
	result, err = walkFiles(root, p)
	if err != nil {
		t.Fatal(err)
	}
	bounded, err := json.Marshal(result)
	if err != nil || string(bounded) != `{"entries":[],"errors":[],"truncated":true}` {
		t.Fatalf("bounded walk response = %s, %v", bounded, err)
	}
}

func TestWalkStopsAtEntryLimit(t *testing.T) {
	root := t.TempDir()
	var names []string
	for i := range directoryReadBatchSize + 2 {
		name := strconv.Itoa(i)
		names = append(names, name)
		if err := os.WriteFile(filepath.Join(root, name), nil, 0600); err != nil {
			t.Fatal(err)
		}
	}
	var p params
	p.Options.MaxDepth = 1
	p.Options.MaxDirectories = 1
	p.Options.MaxEntries = directoryReadBatchSize + 1
	result, err := walkFiles(root, p)
	if err != nil {
		t.Fatal(err)
	}
	out := result.(map[string]any)
	entries := out["entries"].([]map[string]string)
	if len(entries) != p.Options.MaxEntries || out["truncated"] != true {
		t.Fatalf("walk did not truncate at limit %d: %v", p.Options.MaxEntries, out)
	}
	slices.Sort(names)
	for i, e := range entries {
		if want := uri(filepath.Join(root, names[i])); e["path"] != want {
			t.Fatalf("entry %d path = %s, want %s", i, e["path"], want)
		}
	}
}
