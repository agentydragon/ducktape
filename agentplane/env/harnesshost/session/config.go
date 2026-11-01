package session

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/harness"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/journal"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/retained"
	"github.com/agentydragon/ducktape/agentplane/env/scripts"
	"github.com/agentydragon/ducktape/agentplane/env/supervision"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/cosi-project/runtime/pkg/state"
)

type Environment struct {
	IncarnationUID string
	Quiesced       bool
}

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
	WaitBootstrap        func(context.Context) error
	Environment          func() Environment
	EnsureWorkspace      func(context.Context, string) (string, error)
	LaunchProcess        func(context.Context, string, supervision.ProcessConfig) (*supervision.ManagedProcess, error)
	ResolveModelConfig   func(context.Context, runnerpb.Harness, string) (*int, error)
	PublishSeal          func(string, *journal.SealState) error
	PublishAck           func(string, retained.Acknowledgement) error
	OnChange             func()
	OnError              func(error)
	ReconcileConcurrency int
}
