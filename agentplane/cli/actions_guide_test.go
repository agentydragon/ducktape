package main

import (
	"strings"
	"testing"
)

func TestActionsGuideReadsBundledGuidesWithoutServiceCredentials(t *testing.T) {
	cases := []struct {
		name string
		want []string
	}{
		{name: "index", want: []string{"# Agentplane Actions", "/docs/agents/action_lifecycle.md", "get_guide(name=\"index\")"}},
		{name: "session_delegation", want: []string{"# Delegate work to a Session", "agentplane sessions create", "get_guide({\"name\":\"recovery\"})"}},
		{name: "action_lifecycle", want: []string{"# Submit an Action and establish its result", "agentplane actions submit", "An allow decision is not execution success"}},
		{name: "recovery", want: []string{"# Action recovery and cancellation", "never resubmit", "too_late"}},
		{name: "kubernetes", want: []string{"# Kubernetes access and temporary grants", "kubectl --server <host> --token <placeholder>", "PolicyBinding"}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			code, stdout, stderr := invoke(nil, "actions", "guide", test.name)
			if code != 0 || stderr != "" {
				t.Fatalf("guide exit=%d stderr=%q", code, stderr)
			}
			for _, phrase := range test.want {
				if !strings.Contains(stdout, phrase) {
					t.Errorf("guide output lacks %q", phrase)
				}
			}
		})
	}
}

func TestActionsGuideDefaultsToIndexAndSupportsJSON(t *testing.T) {
	code, stdout, stderr := invoke(nil, "actions", "guide")
	if code != 0 || stderr != "" || !strings.Contains(stdout, "# Agentplane Actions") {
		t.Fatalf("index exit=%d stdout=%q stderr=%q", code, stdout, stderr)
	}

	code, stdout, stderr = invoke(nil, "--json", "actions", "guide", "recovery")
	if code != 0 || stderr != "" {
		t.Fatalf("JSON guide exit=%d stderr=%q", code, stderr)
	}
	guide := decodeObject(t, stdout)
	if guide["name"] != "recovery" || !strings.Contains(guide["markdown"].(string), "# Action recovery and cancellation") {
		t.Fatalf("JSON guide = %#v", guide)
	}
}

func TestActionsGuideRejectsUnknownNamesAndAppearsInHelp(t *testing.T) {
	code, _, stderr := invoke(nil, "actions", "guide", "../secrets")
	if code != 2 || !strings.Contains(stderr, "unknown Action guide") {
		t.Fatalf("unknown guide exit=%d stderr=%q", code, stderr)
	}

	code, stdout, stderr := invoke(nil, "actions", "--help")
	if code != 0 || stderr != "" || !strings.Contains(stdout, "guide [NAME]") {
		t.Fatalf("actions help exit=%d stdout=%q stderr=%q", code, stdout, stderr)
	}
}
