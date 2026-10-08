package main

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

// A BES stream in the shape BuildBuddy serves it, exercising the indirection a
// build output sits behind: target -> output group -> file set -> (file set) ->
// files. Set "outer" nests "inner" because Bazel shares subsets between targets
// rather than repeating their files.
const besStream = `[
 {"id":{"namedSet":{"id":"inner"}},
  "namedSetOfFiles":{"files":[
    {"name":"skills/backtrace/backtrace.skill","uri":"bytestream://host/blobs/aaa/12","digest":"aaa","length":"12"}]}},
 {"id":{"namedSet":{"id":"outer"}},
  "namedSetOfFiles":{"files":[
    {"name":"gnome/gterm_theme/gterm_theme.whl","uri":"bytestream://host/blobs/bbb/34","digest":"bbb","length":"34"}],
   "fileSets":[{"id":"inner"}]}},
 {"id":{"namedSet":{"id":"lint"}},
  "namedSetOfFiles":{"files":[
    {"name":"skills/backtrace/report.txt","uri":"bytestream://host/blobs/ccc/5","digest":"ccc","length":"5"}]}},
 {"id":{"namedSet":{"id":"transitioned"}},
  "namedSetOfFiles":{"files":[
    {"pathPrefix":["bazel-out","k8-fastbuild-ST-abc","bin"],"name":"util/testing/frozen-clock.js","uri":"bytestream://host/blobs/eee/9","digest":"eee","length":"9"},
    {"name":"util/testing/frozen-clock.js","uri":"bytestream://host/blobs/fff/9","digest":"fff","length":"9"}]}},
 {"id":{"targetCompleted":{"label":"//util/testing:clock"}},
  "completed":{"success":true,"outputGroup":[{"name":"default","fileSets":[{"id":"transitioned"}]}]}},
 {"id":{"targetCompleted":{"label":"//skills/backtrace:backtrace_skill"}},
  "completed":{"success":true,"outputGroup":[
    {"name":"default","fileSets":[{"id":"outer"}]},
    {"name":"rules_lint_report","fileSets":[{"id":"lint"}]}]}},
 {"id":{"testResult":{"label":"//devinfra/ci:test_release_content_hash"}},
  "testResult":{"testActionOutput":[
    {"name":"test.log","uri":"bytestream://host/blobs/ddd/7","digest":"ddd","length":"7"}]}}
]`

func parseOrFail(t *testing.T, stream string) []artifact {
	t.Helper()
	got, err := parseArtifacts([]byte(stream))
	if err != nil {
		t.Fatalf("parseArtifacts: %v", err)
	}
	return got
}

func find(artifacts []artifact, name string) (artifact, bool) {
	for _, a := range artifacts {
		if a.Name == name {
			return a, true
		}
	}
	return artifact{}, false
}

func TestParseArtifactsFindsBuildOutputs(t *testing.T) {
	got := parseOrFail(t, besStream)
	a, ok := find(got, "gnome/gterm_theme/gterm_theme.whl")
	if !ok {
		t.Fatalf("build output missing from %d artifacts", len(got))
	}
	if a.Kind != kindBuild || a.Label != "//skills/backtrace:backtrace_skill" || a.OutputGroup != "default" {
		t.Errorf("got %+v", a)
	}
	if a.Digest != "bbb" || a.Size != 34 {
		t.Errorf("digest/size not carried through: %+v", a)
	}
}

func TestParseArtifactsFollowsNestedFileSets(t *testing.T) {
	// The .skill lives in a set the target only reaches via another set; missing
	// it would silently hide most outputs of any non-trivial target.
	if _, ok := find(parseOrFail(t, besStream), "skills/backtrace/backtrace.skill"); !ok {
		t.Error("nested file set was not walked")
	}
}

func TestParseArtifactsKeepsTestOutputs(t *testing.T) {
	a, ok := find(parseOrFail(t, besStream), "test.log")
	if !ok {
		t.Fatal("test output lost")
	}
	if a.Kind != kindTest || a.OutputGroup != "" {
		t.Errorf("got %+v", a)
	}
}

func TestParseArtifactsSeparatesOutputGroups(t *testing.T) {
	// Aspects add their own groups, so a caller must be able to tell a target's
	// real outputs from its lint report.
	a, ok := find(parseOrFail(t, besStream), "skills/backtrace/report.txt")
	if !ok {
		t.Fatal("lint output missing")
	}
	if a.OutputGroup != "rules_lint_report" {
		t.Errorf("output group = %q", a.OutputGroup)
	}
}

func TestParseArtifactsDoesNotRepeatSharedFiles(t *testing.T) {
	seen := map[artifact]int{}
	for _, a := range parseOrFail(t, besStream) {
		seen[a]++
	}
	for a, n := range seen {
		if n > 1 {
			t.Errorf("artifact repeated %d times: %+v", n, a)
		}
	}
}

func TestParseArtifactsKeepsFilesDistinctByPathPrefix(t *testing.T) {
	// A source file and the generated file of the same name differ only by prefix
	// (as do outputs of a configuration transition), so dropping the prefix would
	// silently collapse them into one and hand callers the wrong bytes.
	var got []artifact
	for _, a := range parseOrFail(t, besStream) {
		if a.Name == "util/testing/frozen-clock.js" {
			got = append(got, a)
		}
	}
	if len(got) != 2 {
		t.Fatalf("want 2 same-named files, got %d: %+v", len(got), got)
	}
	prefixes := map[string]string{got[0].PathPrefix: got[0].Digest, got[1].PathPrefix: got[1].Digest}
	if prefixes["bazel-out/k8-fastbuild-ST-abc/bin"] != "eee" || prefixes[""] != "fff" {
		t.Errorf("prefix/digest pairing wrong: %+v", prefixes)
	}
}

func TestFilterKind(t *testing.T) {
	got := parseOrFail(t, besStream)
	build, err := filterKind(got, kindBuild)
	if err != nil {
		t.Fatal(err)
	}
	for _, a := range build {
		if a.Kind != kindBuild {
			t.Errorf("kind filter leaked %+v", a)
		}
	}
	if len(build) == 0 || len(build) == len(got) {
		t.Errorf("filter kept %d of %d", len(build), len(got))
	}
	if all, err := filterKind(got, ""); err != nil || len(all) != len(got) {
		t.Errorf("empty kind must keep everything: %d/%d %v", len(all), len(got), err)
	}
	if _, err := filterKind(got, "logs"); err == nil {
		t.Error("an unknown kind must be an error, not an empty result")
	}
}

// A sharded target reports one result per shard, each with a test.log of the same name; only the result's id
// and status tell them apart. Shards count from 1, as they do in real streams. The summary, last, says how
// many there were.
const shardedStream = `[
 {"id":{"testResult":{"label":"//pkg:visual","run":1,"shard":1,"attempt":1}},
  "testResult":{"status":"PASSED","testActionOutput":[
    {"name":"test.log","uri":"bytestream://host/blobs/s1/1","digest":"s1","length":"1"},
    {"name":"test.xml","uri":"bytestream://host/blobs/x1/1","digest":"x1","length":"1"}]}},
 {"id":{"testResult":{"label":"//pkg:visual","run":1,"shard":2,"attempt":1}},
  "testResult":{"status":"FAILED","testActionOutput":[
    {"name":"test.log","uri":"bytestream://host/blobs/s2/1","digest":"s2","length":"1"}]}},
 {"id":{"testResult":{"label":"//pkg:visual","run":1,"shard":3,"attempt":1}},
  "testResult":{"status":"PASSED","testActionOutput":[
    {"name":"test.log","uri":"bytestream://host/blobs/s3/1","digest":"s3","length":"1"}]}},
 {"id":{"testResult":{"label":"//pkg:other","run":1,"shard":1,"attempt":1}},
  "testResult":{"status":"PASSED","testActionOutput":[
    {"name":"test.log","uri":"bytestream://host/blobs/o1/1","digest":"o1","length":"1"}]}},
 {"id":{"testSummary":{"label":"//pkg:visual"}},
  "testSummary":{"overallStatus":"FAILED","shardCount":3,"runCount":1}}
]`

func logsOf(artifacts []artifact, label string) []artifact {
	var logs []artifact
	for _, a := range artifacts {
		if a.Label == label && a.Name == "test.log" {
			logs = append(logs, a)
		}
	}
	return logs
}

func describeAll(artifacts []artifact) []string {
	var described []string
	for _, a := range artifacts {
		described = append(described, a.describe())
	}
	return described
}

func TestParseArtifactsKeepsShardsApart(t *testing.T) {
	// Three logs named alike, which a reader told apart by nothing would take for one.
	got := describeAll(logsOf(parseOrFail(t, shardedStream), "//pkg:visual"))
	want := []string{
		"//pkg:visual (shard 1/3, PASSED)  test.log",
		"//pkg:visual (shard 2/3, FAILED)  test.log",
		"//pkg:visual (shard 3/3, PASSED)  test.log",
	}
	if len(got) != len(want) {
		t.Fatalf("got %v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("log %d = %q, want %q", i, got[i], want[i])
		}
	}
}

func TestUnshardedTargetsAreNotDescribedAsShards(t *testing.T) {
	// With no summary there is no shard count, and a lone result has no shard worth naming.
	other := logsOf(parseOrFail(t, shardedStream), "//pkg:other")
	if len(other) != 1 || other[0].result() != "" || other[0].describe() != "//pkg:other (PASSED)  test.log" {
		t.Errorf("got %+v", other)
	}
}

func TestFailedMeansAResultThatDidNotPass(t *testing.T) {
	for status, want := range map[string]bool{
		"FAILED": true, "TIMEOUT": true, "INCOMPLETE": true, "REMOTE_FAILURE": true,
		"PASSED": false, "FLAKY": false, "NO_STATUS": false, "": false,
	} {
		if got := (artifact{Kind: kindTest, Status: status}).failed(); got != want {
			t.Errorf("failed() for %q = %v, want %v", status, got, want)
		}
	}
	if (artifact{Kind: kindBuild, Status: "FAILED"}).failed() {
		t.Error("a build artifact has no test result to have failed")
	}
}

func TestFilterResults(t *testing.T) {
	all := append(parseOrFail(t, besStream), parseOrFail(t, shardedStream)...)
	if got := filterResults(all, 0, false); len(got) != len(all) {
		t.Errorf("no filter must keep everything, build artifacts too: %d of %d", len(got), len(all))
	}
	second := filterResults(all, 2, false)
	if len(second) != 1 || second[0].Shard != 2 || second[0].Kind != kindTest {
		t.Errorf("shard 2 = %+v", second)
	}
	failed := filterResults(all, 0, true)
	if len(failed) != 1 || failed[0].Status != "FAILED" {
		t.Errorf("failed = %+v", failed)
	}
	if got := filterResults(all, 3, true); len(got) != 0 {
		t.Errorf("shard 3 passed, so it cannot also be the failed one: %+v", got)
	}
}

func TestDownloadNamesKeepShardLogsFromOverwritingEachOther(t *testing.T) {
	arts := parseOrFail(t, shardedStream)
	names := downloadNames(arts)
	seen := map[string]bool{}
	for i, name := range names {
		if seen[name] {
			t.Errorf("%q would be written twice", name)
		}
		seen[name] = true
		if arts[i].Name == "test.xml" && name != "test.xml" {
			t.Errorf("a name nothing else shares stays as it is, got %q", name)
		}
	}
	if !seen["pkg_visual__shard_2_of_3__test.log"] || !seen["pkg_other__test.log"] {
		t.Errorf("names do not say where each came from: %v", names)
	}
}

func TestDownloadNamesLastResortKeepsEveryFile(t *testing.T) {
	// Nothing about these tells them apart but their prefix, which the name does not carry.
	arts := []artifact{{Label: "//a:b", Name: "x/out.txt"}, {Label: "//a:b", Name: "y/out.txt"}}
	if names := downloadNames(arts); names[0] == names[1] {
		t.Errorf("names collide: %v", names)
	}
}

func TestNearArtifacts(t *testing.T) {
	arts := []artifact{
		{Label: "//v:visual", Name: "test.outputs/session_recovery_tools_open-actual.png"},
		{Label: "//v:visual", Name: "test.outputs/session-shell-calls-actual.png"},
	}
	// A name guessed with the other of '-' and '_' than the output uses.
	near := nearArtifacts(arts, "session-recovery-tools-open-actual.png", 5)
	if len(near) != 1 || near[0].Name != arts[0].Name {
		t.Errorf("near = %+v", near)
	}
	if got := nearArtifacts(arts, "nothing-like-it", 5); len(got) != 0 {
		t.Errorf("near = %+v", got)
	}
	if got := nearArtifacts(arts, "*.png", 5); got != nil {
		t.Errorf("a glob names what it means: %+v", got)
	}
}

func TestArtifactAndToolLogDownloadsIncludeInvocationID(t *testing.T) {
	const (
		invocationID = "old-invocation-id"
		uri          = "bytestream://cache.example/instance/blobs/abc123/7"
		payload      = "log data"
	)

	artifactStream := `[{"id":{"testResult":{"label":"//pkg:test"}},"testResult":{"testActionOutput":[{"name":"test.log","uri":"` + uri + `","length":"7"}]}}]`
	toolLogStream := `[{"buildToolLogs":{"log":[{"name":"command.profile.gz","uri":"` + uri + `"}]}}]`
	for _, test := range []struct {
		name       string
		stream     string
		downloaded func(*testing.T, *client) ([]byte, error)
	}{
		{
			name:   "artifact cat",
			stream: artifactStream,
			downloaded: func(t *testing.T, c *client) ([]byte, error) {
				artifacts, err := listArtifacts(c, invocationID)
				if err != nil {
					return nil, err
				}
				if len(artifacts) != 1 {
					t.Fatalf("got %d artifacts, want 1", len(artifacts))
				}
				return captureStdout(t, func() error { return printArtifact(c, artifacts[0]) })
			},
		},
		{
			name:   "artifact download",
			stream: artifactStream,
			downloaded: func(t *testing.T, c *client) ([]byte, error) {
				artifacts, err := listArtifacts(c, invocationID)
				if err != nil {
					return nil, err
				}
				if len(artifacts) != 1 {
					t.Fatalf("got %d artifacts, want 1", len(artifacts))
				}
				path := filepath.Join(t.TempDir(), "test.log")
				if err := saveArtifact(c, artifacts[0], path); err != nil {
					return nil, err
				}
				return os.ReadFile(path)
			},
		},
		{
			name:   "tool log",
			stream: toolLogStream,
			downloaded: func(t *testing.T, c *client) ([]byte, error) {
				logs, err := listToolLogs(c, invocationID)
				if err != nil {
					return nil, err
				}
				if len(logs) != 1 {
					t.Fatalf("got %d tool logs, want 1", len(logs))
				}
				return readToolLog(c, logs[0])
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				query := r.URL.Query()
				if r.URL.Path != "/file/download" {
					t.Errorf("request path = %q, want /file/download", r.URL.Path)
					http.Error(w, "unexpected path", http.StatusBadRequest)
					return
				}
				if query.Get("artifact") == "raw_json" {
					if query.Get("invocation_id") != invocationID {
						t.Errorf("raw BES invocation_id = %q, want %q", query.Get("invocation_id"), invocationID)
						http.Error(w, "missing invocation_id", http.StatusBadRequest)
						return
					}
					_, _ = io.WriteString(w, test.stream)
					return
				}
				if query.Get("invocation_id") == "" {
					http.Error(w, "Missing invocation_id param", http.StatusBadRequest)
					return
				}
				if got := query.Get("invocation_id"); got != invocationID {
					t.Errorf("download invocation_id = %q, want %q", got, invocationID)
					http.Error(w, "wrong invocation_id", http.StatusBadRequest)
					return
				}
				if got := query.Get("bytestream_url"); got != uri {
					t.Errorf("bytestream_url = %q, want %q", got, uri)
					http.Error(w, "wrong bytestream_url", http.StatusBadRequest)
					return
				}
				_, _ = io.WriteString(w, payload)
			}))
			defer server.Close()

			c := &client{baseURL: server.URL, http: server.Client()}
			got, err := test.downloaded(t, c)
			if err != nil {
				t.Fatalf("download: %v", err)
			}
			if !bytes.Equal(got, []byte(payload)) {
				t.Errorf("downloaded %q, want %q", got, payload)
			}
		})
	}
}

func captureStdout(t *testing.T, run func() error) ([]byte, error) {
	t.Helper()
	reader, writer, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	oldStdout := os.Stdout
	os.Stdout = writer
	runErr := run()
	closeErr := writer.Close()
	os.Stdout = oldStdout
	output, readErr := io.ReadAll(reader)
	reader.Close()
	if runErr != nil {
		return nil, runErr
	}
	if closeErr != nil {
		return nil, closeErr
	}
	if readErr != nil {
		return nil, readErr
	}
	return output, nil
}
