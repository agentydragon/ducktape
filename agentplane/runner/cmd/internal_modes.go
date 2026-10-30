package main

import (
	"fmt"
	"os"

	"github.com/agentydragon/ducktape/agentplane/runner/configuration"
	"github.com/agentydragon/ducktape/agentplane/runner/executor"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/agentydragon/ducktape/agentplane/runner/workspace"
)

const (
	executorMode             = "executor"
	vmResourcePropertiesMode = "--vm-resource-properties"
)

// internalMode dispatches private process modes before the runner starts its service.
// args excludes argv[0].
func internalMode(args []string) (handled bool, exitCode int) {
	if len(args) == 0 {
		return false, 0
	}
	switch args[0] {
	case executorMode:
		return true, executor.Main(args[1:])
	case supervision.SupervisorMode:
		return true, supervision.SupervisorMain(args[1:])
	case workspace.HelperMode:
		return true, workspace.Main(args[1:])
	case vmResourcePropertiesMode:
		return true, vmResourcePropertiesMain(args[1:])
	default:
		return false, 0
	}
}

// vmResourcePropertiesMain validates the shared Runner config and prints the
// systemd properties that may be applied to the Runner service.
func vmResourcePropertiesMain(args []string) int {
	if len(args) != 1 || args[0] == "" {
		fmt.Fprintln(os.Stderr, "--vm-resource-properties requires one Runner config path")
		return 64
	}
	properties, err := configuration.GuestResourceSystemdProperties(args[0])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	for _, property := range properties {
		if _, err := fmt.Fprintln(os.Stdout, property); err != nil {
			return 1
		}
	}
	return 0
}
