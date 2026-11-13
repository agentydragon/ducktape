package workloadid

import "testing"

func TestParseWorkloadURIAcceptsOnlyTheCanonicalForm(t *testing.T) {
	workload := ID{Namespace: "agent-ns", Name: "test-agent.box", CredentialID: "0123456789abcdef0123456789abcdef"}
	valid, err := workload.URI()
	if err != nil {
		t.Fatal(err)
	}
	if valid != "spiffe://agentplane/agent-ns/test-agent.box/0123456789abcdef0123456789abcdef" {
		t.Fatalf("URI = %q", valid)
	}
	if parsed, err := Parse(valid); err != nil || parsed != workload {
		t.Fatalf("parsed %+v, %v", parsed, err)
	}
	for _, invalid := range []string{
		"spiffe://agentplane/agent-ns/test-agent",
		"spiffe://agentplane/agent-ns/test-agent/0123456789abcdef0123456789abcdef/extra",
		"spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/0123456789abcdef0123456789abcdef",
		"spiffe://agentplane/Agent-ns/test-agent/0123456789abcdef0123456789abcdef",
		"spiffe://agentplane/agent-ns/Test-agent/0123456789abcdef0123456789abcdef",
		"spiffe://agentplane/agent-ns/test-agent/0123456789ABCDEF0123456789abcdef",
		"spiffe://agentplane/agent-ns/test-agent/credential.id",
		"spiffe://agentplane/agent-ns/-test-agent/0123456789abcdef0123456789abcdef",
		"spiffe://other/agent-ns/test-agent/0123456789abcdef0123456789abcdef",
		"spiffe://agentplane/actions",
	} {
		if _, err := Parse(invalid); err == nil {
			t.Errorf("Parse accepted %q", invalid)
		}
	}
}
