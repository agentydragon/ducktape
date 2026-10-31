package runner

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/execution/companion"
	"github.com/agentydragon/ducktape/agentplane/env/supervision"
	"github.com/agentydragon/ducktape/agentplane/runner/workspace"
)

func TestMain(m *testing.M) {
	// Native process tests re-exec this test binary as the relevant helper.
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "executor":
			os.Exit(executor.Main(os.Args[2:]))
		case supervision.SupervisorMode:
			os.Exit(supervision.SupervisorMain(os.Args[2:]))
		case workspace.HelperMode:
			os.Exit(workspace.Main(os.Args[2:]))
		}
	}
	os.Exit(m.Run())
}
