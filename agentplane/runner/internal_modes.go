package runner

import (
	"fmt"
	"os"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
)

const (
	workspaceMode            = "--agentplane-runner-workspace"
	vmResourcePropertiesMode = "--vm-resource-properties"
	stopGrace                = 5 * time.Second
)

// InternalMode dispatches the private child modes before the runner starts its service.
// args excludes argv[0].
func InternalMode(args []string) (handled bool, exitCode int) {
	if len(args) == 0 {
		return false, 0
	}
	switch args[0] {
	case supervision.SupervisorMode:
		return true, supervision.SupervisorMain(args[1:])
	case workspaceMode:
		return true, WorkspaceMain(args[1:])
	case vmResourcePropertiesMode:
		return true, VMResourcePropertiesMain(args[1:])
	default:
		return false, 0
	}
}

// VMResourcePropertiesMain validates the shared Runner config and prints the
// systemd properties that may be applied to the Runner service.
func VMResourcePropertiesMain(args []string) int {
	if len(args) != 1 || args[0] == "" {
		fmt.Fprintln(os.Stderr, "--vm-resource-properties requires one Runner config path")
		return 64
	}
	properties, err := guestResourceSystemdProperties(args[0])
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
