package main

import "testing"

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
