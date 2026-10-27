package main

import (
	"context"
	"errors"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/sandbox"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
)

var kube *kubeapiserver.Server

// TestMain runs one API server for the sandbox groups' tests; each works in a namespace of its own.
func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(started *kubeapiserver.Server) error {
		kube = started
		return nil
	}))
}

type unusedExecutor struct{}

func (*unusedExecutor) Execute(context.Context, store.ExecutionRequest, *worker.Lease) (store.ExecutionOutcome, error) {
	panic("routing runs no Execution")
}

func TestAnActionRoutesToItsGroupsExecutorWhileTheCatalogOffersIt(t *testing.T) {
	groups := newCatalog(t, `{
		"test-group": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"},
			"actions": {"test-action": {"description": "d"}}},
		"test-offline": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"},
			"available": false, "actions": {"test-action": {"description": "d"}}},
		"test-unbound": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"},
			"actions": {"test-action": {"description": "d"}}}
	}`)
	executor := &unusedExecutor{}
	router := executors{catalog: groups, byGroup: map[string]worker.Executor{"test-group": executor, "test-offline": executor}}
	for _, route := range []struct {
		group, action string
		// runs is whether the Execution goes to the executor rather than failing as unsupported.
		runs, ready bool
	}{
		{"test-group", "test-action", true, true},
		{"test-group", "test-unknown", false, false},
		{"test-offline", "test-action", true, false},
		{"test-unbound", "test-action", false, false},
		{"test-unknown", "test-action", false, false},
	} {
		got, ready := router.Route(store.ActionIdentity{Group: route.group, Name: route.action})
		if (got == executor) != route.runs || (got == nil) == route.runs || ready != route.ready {
			t.Errorf("Route(%s/%s) = %v, %v; want it run = %v, ready = %v", route.group, route.action, got, ready,
				route.runs, route.ready)
		}
	}
}

func TestASandboxGroupOffersTheSandboxActionsAndRunsThem(t *testing.T) {
	groups := sandboxGroups(t, sandboxNamespace(t, kube, true), `["create"]`)
	byGroup, err := sandboxExecutors(t.Context(), groups, kube.Config)
	if err != nil {
		t.Fatal(err)
	}
	if _, bound := byGroup["test-mcp"]; bound || len(byGroup) != 1 {
		t.Errorf("executors are bound to %v, want the sandbox group alone", byGroup)
	}
	router := executors{catalog: groups, byGroup: byGroup}
	if executor, ready := router.Route(store.ActionIdentity{Group: "test-sandbox", Name: "create"}); executor == nil || !ready {
		t.Errorf("create routes to %v, %t; want the sandbox group's executor, ready", executor, ready)
	}
}

func TestAStartupCheckRefusesASandboxGroup(t *testing.T) {
	unsafe := func(err error) bool {
		var refusal *sandbox.TemplateError
		return errors.As(err, &refusal)
	}
	for _, test := range []struct {
		name        string
		sidecar     bool
		directTools string
		refused     func(error) bool
	}{
		{"whose template lets a box escape the egress path", false, "null", unsafe},
		{"whose direct tools name an Action it does not offer", true, `["create", "test-unoffered"]`, func(err error) bool { return err != nil }},
	} {
		t.Run(test.name, func(t *testing.T) {
			groups := sandboxGroups(t, sandboxNamespace(t, kube, test.sidecar), test.directTools)
			if byGroup, err := sandboxExecutors(t.Context(), groups, kube.Config); !test.refused(err) {
				t.Errorf("sandboxExecutors() = %v, %v; want it refused", byGroup, err)
			}
		})
	}
}

func TestAnOrchestrationGroupOffersItsActionsAndRunsThem(t *testing.T) {
	groups := newCatalog(t, `{
		"test-orchestration": {"title": "t", "description": "d", "direct_tools": ["launch_sandbox"],
			"executor": {"kind": "orchestration_v1", "description": "e", "namespace": "test-agentplane"}},
		"test-unknown-tool": {"title": "t", "description": "d", "direct_tools": ["test-unoffered"],
			"executor": {"kind": "orchestration_v1", "description": "e", "namespace": "test-agentplane"}}
	}`)
	if _, err := orchestrationExecutors(groups, kube.Config, nil); err == nil {
		t.Error("a group whose direct tools name an Action it does not offer was accepted")
	}
	groups = newCatalog(t, `{"test-orchestration": {"title": "t", "description": "d", "direct_tools": ["launch_sandbox"],
		"executor": {"kind": "orchestration_v1", "description": "e", "namespace": "test-agentplane"}}}`)
	byGroup, err := orchestrationExecutors(groups, kube.Config, nil)
	if err != nil {
		t.Fatal(err)
	}
	router := executors{catalog: groups, byGroup: byGroup}
	if executor, ready := router.Route(store.ActionIdentity{Group: "test-orchestration", Name: "launch_sandbox"}); executor == nil || !ready {
		t.Errorf("launch_sandbox routes to %v, %t; want the orchestration group's executor, ready", executor, ready)
	}
}
