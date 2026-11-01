package harnesshost

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/workspace"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
)

func TestMain(m *testing.M) {
	// Native process tests re-exec this test binary as the relevant helper.
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "--host-boundary-child":
			os.Exit(hostBoundaryChild(os.Args[2]))
		case supervision.SupervisorMode:
			os.Exit(supervision.SupervisorMain(os.Args[2:]))
		case workspace.HelperMode:
			os.Exit(workspace.Main(os.Args[2:]))
		}
	}
	os.Exit(m.Run())
}
