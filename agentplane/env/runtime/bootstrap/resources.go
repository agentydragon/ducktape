// Package bootstrap owns a Sandbox's durable one-shot initialization attempt.
package bootstrap

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	persistencepb "github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate/persistentpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	RequestType        = "BootstrapRequests.agentplane"
	StatusType         = "BootstrapStatuses.agentplane"
	OperationStateType = "BootstrapOperationStates.agentplane"
	SelectionType      = "BootstrapSelections.agentplane"
	ControllerName     = "bootstrap-lifecycle"
	ResourceID         = "bootstrap"
	HostOwner          = "bootstrap-host"
)

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

type operationState struct{ Done bool }

type componentSpec[T any] interface {
	comparable
	typed.DeepCopyable[T]
	resourceType() resource.Type
}

type componentExtension[T componentSpec[T]] struct{}

func (componentExtension[T]) ResourceDefinition() spec.ResourceDefinitionSpec {
	var value T
	return spec.ResourceDefinitionSpec{Type: value.resourceType(), DefaultNamespace: lifecycle.Namespace}
}

func componentResource[T componentSpec[T]](id string, value T) *typed.Resource[T, componentExtension[T]] {
	return typed.NewResource[T, componentExtension[T]](
		resource.NewMetadata(lifecycle.Namespace, value.resourceType(), id, resource.VersionUndefined), value,
	)
}

func componentRead[T componentSpec[T]](ctx context.Context, reader controller.Reader, id string) (T, bool, error) {
	var zero T
	return cosi.Read(ctx, reader, componentResource(id, zero))
}

func componentWrite[T componentSpec[T]](ctx context.Context, writer controller.ReaderWriter, id string, value T) (bool, error) {
	return cosi.Write(ctx, writer, componentResource(id, value), value)
}

func publish[T componentSpec[T]](ctx context.Context, writer state.State, id string, value T) error {
	return cosi.Publish(ctx, writer, componentResource(id, value), value, HostOwner)
}

func ReadRequest(ctx context.Context, reader controller.Reader) (Request, bool, error) {
	return componentRead[Request](ctx, reader, ResourceID)
}

func ReadStatus(ctx context.Context, reader controller.Reader) (Status, bool, error) {
	return componentRead[Status](ctx, reader, ResourceID)
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

func isTerminal(phase Phase) bool {
	return phase == Succeeded || phase == Failed || phase == Interrupted
}

func (Request) resourceType() resource.Type           { return RequestType }
func (value Request) DeepCopy() Request               { return value }
func (Status) resourceType() resource.Type            { return StatusType }
func (value Status) DeepCopy() Status                 { return value }
func (operationState) resourceType() resource.Type    { return OperationStateType }
func (value operationState) DeepCopy() operationState { return value }
