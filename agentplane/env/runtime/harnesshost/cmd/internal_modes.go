package main

import (
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/workspace"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
)

// internalMode handles native process supervision helpers before serving Sessions.
func internalMode(args []string) (bool, int) {
	if len(args) == 0 {
		return false, 0
	}
	switch args[0] {
	case supervision.SupervisorMode:
		return true, supervision.SupervisorMain(args[1:])
	case workspace.HelperMode:
		return true, workspace.Main(args[1:])
	default:
		return false, 0
	}
}
