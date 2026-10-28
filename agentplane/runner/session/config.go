package session

import (
	"context"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/runner/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/agentydragon/ducktape/agentplane/runner/scripts"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/state"
)

type DebugCheckpointConfig struct{ Name, CommandID string }

// Config names the concrete facilities one Session needs and the narrow
// Runner-wide admission/publication gates retained by the aggregate owner.
type Config struct {
	Context              context.Context
	State                state.State
	Store                *retained.Store
	WorkspaceRoot        string
	Destination          func(string) string
	Claude               *harness.LaunchConfig
	HarnessEnvironment   map[string]string
	DebugCheckpoint      *DebugCheckpointConfig
	Scripts              *scripts.Runner
	CodexAppServer       *codexappserver.Host
	BootstrapHost        *bootstrap.Host
	EnsureWorkspace      func(context.Context, string) (string, error)
	LaunchProcess        func(context.Context, string, supervision.ProcessConfig) (*supervision.ManagedProcess, error)
	ResolveModelConfig   func(context.Context, runnerpb.Harness, string) (*int, error)
	PublishSeal          func(string, *journal.SealState) error
	PublishAck           func(string, retained.Acknowledgement) error
	OnChange             func()
	OnError              func(error)
	ReconcileConcurrency int
}
