package main

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	"gopkg.in/yaml.v3"
)

type unusedExecutor struct{}

func (*unusedExecutor) Execute(context.Context, store.ExecutionRequest, *worker.Lease) (store.ExecutionOutcome, error) {
	panic("routing runs no Execution")
}

func TestAnActionRoutesToItsGroupsExecutorWhileTheCatalogOffersIt(t *testing.T) {
	var settings catalog.Settings
	if err := yaml.Unmarshal([]byte(`{
		"test-group": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"},
			"actions": {"test-action": {"description": "d"}}},
		"test-offline": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"},
			"available": false, "actions": {"test-action": {"description": "d"}}},
		"test-unbound": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"},
			"actions": {"test-action": {"description": "d"}}}
	}`), &settings); err != nil {
		t.Fatal(err)
	}
	groups, err := catalog.New(settings)
	if err != nil {
		t.Fatal(err)
	}
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
