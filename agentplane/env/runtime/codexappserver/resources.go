package codexappserver

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	LifecycleSignalType = "CodexAppServerLifecycleSignals.agentplane"
	StatusType          = "CodexAppServerStatuses.agentplane"
	ControllerName      = "codex-app-server-lifecycle"
	ResourceID          = "codex-app-server"
	HostOwner           = "codex-app-server-host"
)

type LifecycleSignal struct {
	OperationRevision uint64
	Demand            uint64
	ExitSequence      uint64
	ExitProcessID     string
	ExitError         string
}

type Phase uint8

const (
	Stopped Phase = iota
	Initializing
	Ready
	Draining
)

type Status struct {
	Phase                   Phase
	EnvironmentGeneration   uint64
	ObservedDemand          uint64
	ExitSequence            uint64
	ExecServerReadyDeadline time.Time
	ProcessID               string
	Error                   string
	StopError               string
}

func ReadLifecycleSignal(ctx context.Context, reader controller.Reader) (LifecycleSignal, bool, error) {
	return lifecycle.Read[LifecycleSignal](ctx, reader, ResourceID)
}

func ReadStatus(ctx context.Context, reader controller.Reader) (Status, bool, error) {
	return lifecycle.Read[Status](ctx, reader, ResourceID)
}

func (LifecycleSignal) ResourceType() resource.Type     { return LifecycleSignalType }
func (value LifecycleSignal) DeepCopy() LifecycleSignal { return value }
func (Status) ResourceType() resource.Type              { return StatusType }
func (value Status) DeepCopy() Status                   { return value }

// Mutations preserve independent operation wakeups across demand resets and exit updates.
func (c *Host) updateLifecycleSignal(ctx context.Context, update func(*LifecycleSignal)) error {
	_, err := safe.StateUpdateWithConflicts(ctx, c.deps.State, lifecycle.ResourceFor(ResourceID, LifecycleSignal{}).Metadata(), func(current *typed.Resource[LifecycleSignal, lifecycle.Extension[LifecycleSignal]]) error {
		update(current.TypedSpec())
		return nil
	}, state.WithUpdateOwner(HostOwner))
	return err
}
