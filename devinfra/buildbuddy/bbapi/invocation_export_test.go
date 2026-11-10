package main

import (
	"path"
	"strings"
	"testing"

	bbapipb "github.com/agentydragon/ducktape/devinfra/buildbuddy/bbapi/proto"
	bespb "github.com/buildbuddy-io/buildbuddy/proto/build_event_stream"
	"google.golang.org/protobuf/encoding/protojson"
)

const invocationExportStream = `[
 {"id":{"namedSet":{"id":"inner"}},
  "namedSetOfFiles":{"files":[
    {"pathPrefix":["bazel-out","k8-fastbuild","bin"],"name":"pkg/app.whl",
     "uri":"bytestream://host/blobs/app/12","digest":"app","length":"12"},
    {"name":"pkg/same.txt","uri":"bytestream://host/blobs/source/3","digest":"source","length":"3"}]}},
 {"id":{"namedSet":{"id":"left"}},
  "namedSetOfFiles":{"fileSets":[{"id":"inner"}]}},
 {"id":{"namedSet":{"id":"right"}},
  "namedSetOfFiles":{"fileSets":[{"id":"inner"}],"files":[
    {"pathPrefix":["bazel-out","k8-fastbuild","bin"],"name":"pkg/same.txt",
     "uri":"bytestream://host/blobs/generated/3","digest":"generated","length":"3"}]}},
 {"id":{"namedSet":{"id":"lint"}},
  "namedSetOfFiles":{"files":[
    {"pathPrefix":["bazel-out","k8-fastbuild","bin"],"name":"pkg/app.mypy_stdout",
     "uri":"bytestream://host/blobs/lint/5","digest":"lint","length":"5"}]}},
 {"id":{"targetCompleted":{"label":"//pkg:app"}},
  "completed":{"outputGroup":[{"name":"default","fileSets":[{"id":"left"},{"id":"right"}]}]}},
 {"id":{"targetCompleted":{"label":"//pkg:app","aspect":"//devinfra/lint:linters.bzl%mypy_aspect"}},
  "completed":{"outputGroup":[{"name":"mypy","fileSets":[{"id":"lint"}]}]}},
 {"id":{"testSummary":{"label":"//pkg:test","configuration":{"id":"cfg-one"}}},
  "testSummary":{"overallStatus":"PASSED","attemptCount":0,"totalNumCached":"0"}},
 {"id":{"testSummary":{"label":"//pkg:test","configuration":{"id":"cfg-two"}}},
  "testSummary":{"overallStatus":"FLAKY"}},
 {"started":{"command":"test"}},
 {"id":{"pattern":{"pattern":["//pkg:...","//other:target"]}}},
 {"optionsParsed":{"explicitCmdLine":["test","//pkg:..."]}},
 {"workspaceStatus":{"item":[{"key":"COMMIT_SHA","value":"deadbeef"}]}},
 {"finished":{"exitCode":{"name":"SUCCESS"}}}
]`

func TestParseInvocationExportWalksFileSetDAGAndKeepsFullPaths(t *testing.T) {
	got, err := parseInvocationExport([]byte(invocationExportStream))
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Outputs) != 4 {
		t.Fatalf("got %d outputs, want one app wheel, two same-named files and one aspect report: %+v", len(got.Outputs), got.Outputs)
	}
	byPath := map[string]*bbapipb.Artifact{}
	for _, output := range got.Outputs {
		byPath[path.Join(append(output.GetFile().GetPathPrefix(), output.GetFile().GetName())...)] = output
	}
	if byPath["bazel-out/k8-fastbuild/bin/pkg/app.whl"].GetFile().GetDigest() != "app" {
		t.Errorf("nested wheel output missing or wrong: %+v", byPath)
	}
	if byPath["pkg/same.txt"].GetFile().GetDigest() != "source" || byPath["bazel-out/k8-fastbuild/bin/pkg/same.txt"].GetFile().GetDigest() != "generated" {
		t.Errorf("same-named files were collapsed across path prefixes: %+v", byPath)
	}
	if byPath["bazel-out/k8-fastbuild/bin/pkg/app.mypy_stdout"].GetId().GetTargetCompleted().GetAspect() != "//devinfra/lint:linters.bzl%mypy_aspect" ||
		byPath["bazel-out/k8-fastbuild/bin/pkg/app.mypy_stdout"].GetOutputGroup() != "mypy" {
		t.Errorf("aspect output metadata missing: %+v", byPath)
	}
	for _, output := range got.Outputs {
		if output.GetFile().GetName() == "pkg/app.whl" && output.GetFile().GetLength() != 12 {
			t.Errorf("size = %d, want 12", output.GetFile().GetLength())
		}
	}
}

func TestParseInvocationExportPreservesSummaryPresenceAndConfigurations(t *testing.T) {
	got, err := parseInvocationExport([]byte(invocationExportStream))
	if err != nil {
		t.Fatal(err)
	}
	if len(got.TestSummaries) != 2 {
		t.Fatalf("got %d summaries, want both configurations", len(got.TestSummaries))
	}
	first, second := got.TestSummaries[0], got.TestSummaries[1]
	if first.GetId().GetConfiguration().GetId() != "cfg-one" || first.GetSummary().GetOverallStatus() != bespb.TestStatus_PASSED || !first.GetAttemptCountPresent() || first.GetSummary().GetAttemptCount() != 0 || !first.GetCachedCountPresent() || first.GetSummary().GetTotalNumCached() != 0 {
		t.Errorf("first summary lost explicit zero values: %+v", first)
	}
	if second.GetId().GetConfiguration().GetId() != "cfg-two" || second.GetSummary().GetOverallStatus() != bespb.TestStatus_FLAKY || second.GetAttemptCountPresent() || second.GetCachedCountPresent() {
		t.Errorf("second summary should retain absent count fields: %+v", second)
	}
	if got.TestStatus["//pkg:test"] != bespb.TestStatus_FLAKY {
		t.Errorf("test status should retain last summary value: %+v", got.TestStatus)
	}
	encoded, err := protojson.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(encoded), `"attemptCountPresent":true`) || !strings.Contains(string(encoded), `"cachedCountPresent":true`) {
		t.Errorf("JSON does not distinguish absent and zero counts: %s", encoded)
	}
}

func TestParseInvocationExportPreservesUnambiguousProvenance(t *testing.T) {
	got, err := parseInvocationExport([]byte(invocationExportStream))
	if err != nil {
		t.Fatal(err)
	}
	if !got.Finished || !got.Successful {
		t.Errorf("finished/successful = %v/%v, want true/true", got.Finished, got.Successful)
	}
	if got.Command == nil || *got.Command != "test" {
		t.Errorf("command = %v, want test", got.Command)
	}
	if got.RequestedPatterns == nil || strings.Join(got.RequestedPatterns.GetValues(), ",") != "//pkg:...,//other:target" {
		t.Errorf("requested patterns = %v", got.RequestedPatterns)
	}
	if got.ExplicitCmdLine == nil || strings.Join(got.ExplicitCmdLine.GetValues(), " ") != "test //pkg:..." {
		t.Errorf("explicit command line = %v", got.ExplicitCmdLine)
	}
	if got.CommitSha == nil || *got.CommitSha != "deadbeef" {
		t.Errorf("commit SHA = %v", got.CommitSha)
	}
}

func TestParseInvocationExportRejectsAmbiguousProvenance(t *testing.T) {
	stream := `[
 {"started":{"command":"test"}},
 {"started":{"command":"build"}},
 {"optionsParsed":{"explicitCmdLine":["test"]}},
 {"optionsParsed":{"explicitCmdLine":["build"]}},
 {"id":{"pattern":{"pattern":["//pkg:..."]}}},
 {"workspaceStatus":{"item":[{"key":"COMMIT_SHA","value":"one"}]}},
 {"workspaceStatus":{"item":[{"key":"COMMIT_SHA","value":"two"}]}},
 {"finished":{"exitCode":{"name":"SUCCESS"}}},
 {"finished":{"exitCode":{"name":"SUCCESS"}}}
]`
	got, err := parseInvocationExport([]byte(stream))
	if err != nil {
		t.Fatal(err)
	}
	if got.Command != nil || got.ExplicitCmdLine != nil || got.CommitSha != nil {
		t.Errorf("ambiguous provenance was guessed: %+v", got)
	}
	if got.RequestedPatterns == nil || strings.Join(got.RequestedPatterns.GetValues(), ",") != "//pkg:..." {
		t.Errorf("requested patterns = %v", got.RequestedPatterns)
	}
	if got.Finished || got.Successful {
		t.Errorf("two finished events must not be reported as a uniquely finished invocation: %v/%v", got.Finished, got.Successful)
	}
}

func TestParseInvocationExportRejectsNonListInput(t *testing.T) {
	if _, err := parseInvocationExport([]byte(`{"event":true}`)); err == nil {
		t.Fatal("expected non-list stream to be rejected")
	}
}
