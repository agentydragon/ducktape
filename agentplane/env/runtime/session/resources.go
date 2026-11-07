// Package session owns the Runtime's durable Session inventory and its COSI lifecycle.
package session

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
)

const (
	ReconcileSnapshotType = "SessionReconcileSnapshots.agentplane"
	LifecycleStatusType   = "SessionLifecycleStatuses.agentplane"
	OwnerStatusType       = "SessionOwnerStatuses.agentplane"
	ownerRetryType        = "SessionOwnerRetries.agentplane"
	ControllerName        = "session-lifecycle"
)

type Phase uint8

const (
	Dormant Phase = iota
	Preparing
	Ready
	Stopping
	Halted
)

type ReconcileSnapshot struct {
	Harness           sessionpb.Harness
	Demand            uint64
	OperationRevision uint64
	SetupRequested    bool
	Setup             sessionpb.SetupState
	Pending           int
	ProcessID         string
	ProcessRunning    bool
	Inspections       int
	InspectionRunning bool
	InspectionReady   bool
	DispatchBlocked   bool
}

// OwnerStatus reports that the live owner has recovered the durable Session.
type OwnerStatus struct {
	Ready bool
	Error string
}

type ownerRetry struct{ Revision uint64 }

type LifecycleStatus struct {
	Phase                 Phase
	ObservedDemand        uint64
	Resuming              bool
	ProcessID             string
	EnvironmentGeneration uint64
	// AdmissionClosed reports that the cutoff is enforced and accepted commands
	// are settled for EnvironmentGeneration. Identical committed command IDs may
	// still return their original receipt.
	AdmissionClosed  bool
	SetupOperationID string
	ProcessStopped   bool
	// PreFenceSettled is the generation-qualified local shutdown barrier. It
	// becomes true only after halt, native exit callbacks and every resulting
	// inspection have settled. It does not wait for the global Execution fence.
	PreFenceSettled bool
	// Sealed is published only after the journal commit and the current
	// generation's native and Execution fences have been observed.
	Sealed                 bool
	SealUID                string
	SealCursor             uint64
	Delivered              bool
	AcknowledgementPresent bool
	AcknowledgementSealed  bool
	AcknowledgementCursor  uint64
	Error                  string
}

func ReadReconcileSnapshot(ctx context.Context, reader controller.Reader, id string) (ReconcileSnapshot, bool, error) {
	return lifecycle.Read[ReconcileSnapshot](ctx, reader, id)
}

func ReadLifecycleStatus(ctx context.Context, reader controller.Reader, id string) (LifecycleStatus, bool, error) {
	return lifecycle.Read[LifecycleStatus](ctx, reader, id)
}

func (ReconcileSnapshot) ResourceType() resource.Type       { return ReconcileSnapshotType }
func (value ReconcileSnapshot) DeepCopy() ReconcileSnapshot { return value }
func (LifecycleStatus) ResourceType() resource.Type         { return LifecycleStatusType }
func (value LifecycleStatus) DeepCopy() LifecycleStatus     { return value }
func (OwnerStatus) ResourceType() resource.Type             { return OwnerStatusType }
func (value OwnerStatus) DeepCopy() OwnerStatus             { return value }
func (ownerRetry) ResourceType() resource.Type              { return ownerRetryType }
func (value ownerRetry) DeepCopy() ownerRetry               { return value }

func ReadOwnerStatus(ctx context.Context, reader controller.Reader, id string) (OwnerStatus, bool, error) {
	return lifecycle.Read[OwnerStatus](ctx, reader, id)
}
