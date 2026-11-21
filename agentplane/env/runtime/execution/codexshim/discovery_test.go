package codexshim

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/bazelbuild/rules_go/go/runfiles"
	"github.com/coder/websocket"
)

type discoveryProbeRoot struct {
	ID      string          `json:"id"`
	Path    string          `json:"path"`
	Sandbox json.RawMessage `json:"sandbox,omitempty"`
}

type discoveryReply struct {
	ID    int `json:"id"`
	Error *struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
	Result struct {
		Roots []rootResult `json:"roots"`
	} `json:"result"`
}

func TestCapabilityDiscoveryRemote(t *testing.T) {
	root := t.TempDir()
	outside := filepath.Join(t.TempDir(), "outside.json")
	writeDiscoveryFixture(t, outside, `{"name":"outside"}`)
	writeDiscoveryFixture(t, filepath.Join(root, ".codex-plugin/plugin.json"), fmt.Sprintf(`{"mcpServers":%q,"apps":"./config/app %%.json"}`, "./"+outside))
	for path, contents := range map[string]string{
		".mcp.json":                             `{"mcpServers":{}}`,
		"config/app %.json":                     `{"apps":{}}`,
		".hidden/a/b/c/d/e/SKILL.md":            "last scanned depth",
		".hidden/a/b/c/d/e/f/SKILL.md":          "too deep",
		"nested/.claude-plugin/plugin.json":     `{"name":"nested"}`,
		"nested/skills/task/SKILL.md":           "nested skill",
		"nested/skills/task/agents/openai.yaml": "policy: {}",
		"z-original/SKILL.md":                   "directory alias skill",
		"z [literal]!/SKILL.md":                 "URI segment spelling",
	} {
		writeDiscoveryFixture(t, filepath.Join(root, path), contents)
	}
	for path, target := range map[string]string{
		"a-alias":         "z-original",
		"cycle":           ".",
		"skills/SKILL.md": "../z-original/SKILL.md",
		"broken":          "missing",
	} {
		link := filepath.Join(root, path)
		if err := os.MkdirAll(filepath.Dir(link), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(target, link); err != nil {
			t.Fatal(err)
		}
	}

	fullRead := json.RawMessage(fmt.Sprintf(`{"permissions":{"type":"managed","file_system":{"type":"restricted","entries":[{"path":{"type":"special","value":{"kind":"root"}},"access":"read"}]},"network":"restricted"},"windowsSandboxLevel":"disabled","policyContext":{"cwd":%q}}`, uri(root)))
	roots := []discoveryProbeRoot{
		{ID: "full-read", Path: uri(root), Sandbox: fullRead},
		{ID: "inherited", Path: uri(filepath.Join(root, "nested/skills")), Sandbox: json.RawMessage(`{"permissions":{"type":"disabled"},"windowsSandboxLevel":"disabled"}`)},
		{ID: "file", Path: uri(filepath.Join(root, ".mcp.json"))},
	}

	got := discoverOverWebSocket(t, DefaultLimits(), roots)
	want := discoverWithPinnedCodex(t, roots)
	if got.Error != nil || want.Error != nil || !reflect.DeepEqual(got.Result.Roots, want.Result.Roots) {
		t.Fatalf("discovery differs from pinned Codex\nshim: %s\nCodex: %s", describeDiscovery(got), describeDiscovery(want))
	}
	if len(got.Result.Roots) != len(roots) || got.Result.Roots[0].Plugin == nil || len(got.Result.Roots[0].Skills) != 4 || len(got.Result.Roots[1].Namespace) != 1 || got.Result.Roots[2].Error == nil {
		t.Fatalf("expected plugins, four skills, inherited namespace and an isolated root error: %s", describeDiscovery(got))
	}

	// A root-wide grant with a narrower read denial still requires sandbox enforcement.
	restricted := json.RawMessage(fmt.Sprintf(`{"permissions":{"type":"managed","file_system":{"type":"restricted","entries":[{"path":{"type":"special","value":{"kind":"root"}},"access":"read"},{"path":{"type":"path","path":%q},"access":"deny"}]},"network":"restricted"},"windowsSandboxLevel":"disabled"}`, uri(root)))
	blocked := discoverOverWebSocket(t, DefaultLimits(), []discoveryProbeRoot{
		{ID: "blocked", Path: uri(root), Sandbox: restricted},
		roots[1],
	})
	results := blocked.Result.Roots
	if blocked.Error != nil || len(results) != 2 || results[0].Error == nil || results[0].Plugin != nil || len(results[0].Skills) != 0 || len(results[0].Namespace) != 0 || results[1].Error != nil || len(results[1].Skills) != 1 {
		t.Fatalf("blocked discovery leaked data or discarded its unrestricted sibling: %s", describeDiscovery(blocked))
	}
}

func TestCapabilityDiscoveryConfiguredLimits(t *testing.T) {
	root := t.TempDir()
	for path, contents := range map[string]string{
		"SKILL.md":             "large",
		"a/SKILL.md":           "fits",
		"a/agents/openai.yaml": "more",
		"a/deep/SKILL.md":      "deep",
		"b/SKILL.md":           "skip",
	} {
		writeDiscoveryFixture(t, filepath.Join(root, path), contents)
	}
	limits := DefaultLimits()
	limits.MaxDiscoveryRoots = 1
	limits.MaxDiscoveryDepth = 1
	limits.MaxDiscoveryDirectories = 2
	limits.MaxDiscoveryEntries = 5
	limits.MaxDiscoveryFileBytes = 4
	limits.MaxDiscoveryBundleBytes = 4

	request := discoveryProbeRoot{ID: "limited", Path: uri(root)}
	tooMany := discoverOverWebSocket(t, limits, []discoveryProbeRoot{request, request})
	if tooMany.Error == nil || tooMany.Error.Code != -32600 {
		t.Fatalf("configured root cap did not reject request: %s", describeDiscovery(tooMany))
	}
	got := discoverOverWebSocket(t, limits, []discoveryProbeRoot{request})
	if got.Error != nil || len(got.Result.Roots) != 1 {
		t.Fatalf("limited discovery failed: %s", describeDiscovery(got))
	}
	result := got.Result.Roots[0]
	if result.Error != nil || len(result.Skills) != 1 || result.Skills[0].Instructions.Contents != "fits" || result.Skills[0].Metadata != nil || len(result.Warnings) != 3 {
		t.Fatalf("expected one skill and scan, file-size and bundle-size warnings: %s", describeDiscovery(got))
	}

	for _, test := range []struct {
		name                        string
		depth, directories, entries int
		want                        []string
	}{
		{name: "depth", depth: 1, directories: 10, entries: 20, want: []string{"large", "fits", "skip"}},
		{name: "directories", depth: 6, directories: 2, entries: 20, want: []string{"large", "fits"}},
		{name: "entries", depth: 6, directories: 10, entries: 1, want: []string{"large"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			limits := DefaultLimits()
			limits.MaxDiscoveryDepth = test.depth
			limits.MaxDiscoveryDirectories = test.directories
			limits.MaxDiscoveryEntries = test.entries
			got := discoverOverWebSocket(t, limits, []discoveryProbeRoot{request})
			if got.Error != nil || len(got.Result.Roots) != 1 || got.Result.Roots[0].Error != nil {
				t.Fatalf("limited discovery failed: %s", describeDiscovery(got))
			}
			var contents []string
			for _, skill := range got.Result.Roots[0].Skills {
				contents = append(contents, skill.Instructions.Contents)
			}
			if !reflect.DeepEqual(contents, test.want) {
				t.Fatalf("got skill contents %q, want %q", contents, test.want)
			}
		})
	}
}

func writeDiscoveryFixture(t *testing.T, path, contents string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(contents), 0644); err != nil {
		t.Fatal(err)
	}
}

func discoverOverWebSocket(t *testing.T, limits Limits, roots []discoveryProbeRoot) discoveryReply {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	ws := dialShim(t, ctx, serveShim(t, limits))
	return callDiscovery(t, roots, func(request []byte, expectResponse bool) []byte {
		if err := ws.Write(ctx, websocket.MessageText, request); err != nil {
			t.Fatal(err)
		}
		if !expectResponse {
			return nil
		}
		_, body, err := ws.Read(ctx)
		if err != nil {
			t.Fatal(err)
		}
		return body
	})
}

func discoverWithPinnedCodex(t *testing.T, roots []discoveryProbeRoot) discoveryReply {
	t.Helper()
	binary, err := runfiles.Rlocation(codexRlocation)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	home := t.TempDir()
	cmd := exec.CommandContext(ctx, binary, "exec-server", "--listen", "stdio")
	cmd.Dir = home
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "HOME=" + home, "CODEX_HOME=" + home}
	cmd.Stderr = io.Discard
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = stdin.Close()
		_ = cmd.Wait()
	}()

	reader := bufio.NewReader(stdout)
	return callDiscovery(t, roots, func(request []byte, expectResponse bool) []byte {
		if _, err := stdin.Write(append(request, '\n')); err != nil {
			t.Fatal(err)
		}
		if !expectResponse {
			return nil
		}
		body, err := reader.ReadBytes('\n')
		if err != nil {
			t.Fatal(err)
		}
		return body
	})
}

func callDiscovery(t *testing.T, roots []discoveryProbeRoot, exchange func([]byte, bool) []byte) discoveryReply {
	t.Helper()
	call := func(request string, id int) discoveryReply {
		t.Helper()
		body := exchange([]byte(request), id != 0)
		var reply discoveryReply
		if id != 0 {
			if err := json.Unmarshal(body, &reply); err != nil || reply.ID != id {
				t.Fatalf("invalid RPC response: %s (%v)", body, err)
			}
		}
		return reply
	}
	initialized := call(`{"id":1,"method":"initialize","params":{"clientName":"discovery-differential"}}`, 1)
	if initialized.Error != nil {
		t.Fatalf("initialize failed: %+v", initialized.Error)
	}
	call(`{"method":"initialized","params":{}}`, 0)
	encodedRoots, err := json.Marshal(roots)
	if err != nil {
		t.Fatal(err)
	}
	return call(fmt.Sprintf(`{"id":2,"method":"capabilityRoots/discoverV1","params":{"roots":%s}}`, encodedRoots), 2)
}

func describeDiscovery(reply discoveryReply) string {
	body, _ := json.Marshal(reply)
	return string(body)
}
