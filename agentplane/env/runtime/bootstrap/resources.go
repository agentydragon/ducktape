// Package bootstrap owns a Sandbox's durable one-shot initialization attempt.
package bootstrap

import (
	"context"

	persistencepb "github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate/persistentpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	RequestType         = "BootstrapRequests.agentplane"
	StatusType          = "BootstrapStatuses.agentplane"
	OperationSignalType = "BootstrapOperationSignals.agentplane"
	SelectionType       = "BootstrapSelections.agentplane"
	EventType           = "BootstrapEvents.agentplane"
	ControllerName      = "bootstrap-lifecycle"
	ResourceID          = "bootstrap"
	HostOwner           = "bootstrap-host"
)

// oneoff.Operation publishes once before work and once after completion.
const operationCompletionRevision uint64 = 2

// Request records accepted Sandbox bootstrap demand under the Runtime admission lock.
// It is live-only and must never be restored after a restart.
type Request struct {
	Requested bool
}

type Phase uint8

const (
	Dormant Phase = iota
	Running
	Succeeded
	Failed
	Interrupted
)

type Status struct {
	Phase Phase
	Error string
}

type operationSignal struct{ Revision uint64 }

func ReadRequest(ctx context.Context, reader controller.Reader) (Request, bool, error) {
	return lifecycle.Read[Request](ctx, reader, ResourceID)
}

func ReadStatus(ctx context.Context, reader controller.Reader) (Status, bool, error) {
	return lifecycle.Read[Status](ctx, reader, ResourceID)
}

func ReadSelection(ctx context.Context, reader controller.Reader) (string, bool, error) {
	value, err := safe.ReaderGet[*SelectionResource](ctx, reader, SelectionResourceValue("").Metadata())
	if state.IsNotFoundError(err) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	if err := validateScript(string(value.TypedSpec().Value.Script)); err != nil {
		return "", false, err
	}
	return string(value.TypedSpec().Value.Script), true, nil
}

func publishSelection(ctx context.Context, writer state.State, script string) error {
	if err := validateScript(script); err != nil {
		return err
	}
	return safe.StateModify(ctx, writer, SelectionResourceValue(script), func(current *SelectionResource) error {
		current.TypedSpec().Value = &persistencepb.BootstrapSelectionSpec{Script: []byte(script)}
		return nil
	}, state.WithUpdateOwner(HostOwner))
}

func (phase Phase) IsTerminal() bool {
	return phase == Succeeded || phase == Failed || phase == Interrupted
}

func (Request) ResourceType() resource.Type             { return RequestType }
func (value Request) DeepCopy() Request                 { return value }
func (Status) ResourceType() resource.Type              { return StatusType }
func (value Status) DeepCopy() Status                   { return value }
func (operationSignal) ResourceType() resource.Type     { return OperationSignalType }
func (value operationSignal) DeepCopy() operationSignal { return value }
