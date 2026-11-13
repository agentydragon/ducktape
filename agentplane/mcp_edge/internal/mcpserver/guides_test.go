package mcpserver

import (
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/docs"
)

func TestGuideToolReadsSharedAssetsWithoutTheActionBackend(t *testing.T) {
	recorder := &recordingCore{}
	session := newHarness(t, recorder.core(nil, noWatch), Options{MaxWaitSeconds: 30}).connect(t, callerToken)
	for _, name := range []docs.Name{docs.Index, docs.SessionDelegation, docs.ActionLifecycle, docs.Recovery, docs.Kubernetes} {
		t.Run(string(name), func(t *testing.T) {
			arguments, err := json.Marshal(getGuideInput{Name: name})
			if err != nil {
				t.Fatal(err)
			}
			result := callTool(t, session, "get_guide", string(arguments))
			if result.IsError {
				t.Fatal(text(t, result))
			}
			want, err := docs.Read(name)
			if err != nil {
				t.Fatal(err)
			}
			var got docs.Guide
			if err := json.Unmarshal([]byte(text(t, result)), &got); err != nil {
				t.Fatal(err)
			}
			if got != want || !strings.HasPrefix(got.Markdown, "# ") {
				t.Errorf("guide %q does not match the published Markdown asset", name)
			}
			if !reflect.DeepEqual(decoded(t, result.StructuredContent), decoded(t, got)) {
				t.Error("structured guide differs from its text fallback")
			}
		})
	}
	rpcs, callers := recorder.recorded()
	if operations, _ := withoutCatalogRPC(rpcs, callers); len(operations) != 0 {
		t.Errorf("guide reads called Action APIs: %v", operations)
	}
}

func TestGuideNamesCannotSelectFilesOrURLs(t *testing.T) {
	for _, name := range []docs.Name{"", "missing", "../llms.txt", "session_delegation.md", "https://example.com/guide.md"} {
		t.Run(string(name), func(t *testing.T) {
			result, err := (&tools{}).getGuide(t.Context(), getGuideInput{Name: name})
			var refusal toolError
			if result != nil || !errors.As(err, &refusal) || !strings.Contains(refusal.Error(), `get_guide(name="index")`) {
				t.Errorf("get_guide(%q) = %v, %v; want an actionable refusal", name, result, err)
			}
		})
	}
}
