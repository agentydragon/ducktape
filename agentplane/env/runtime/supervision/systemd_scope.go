package supervision

import (
	"context"
	"errors"
	"fmt"
	"sync/atomic"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

const (
	SystemdScopeDesiredType = "SystemdScopeDesiredStates.agentplane"
	SystemdScopeStatusType  = "SystemdScopeStatuses.agentplane"
)

// SystemdScopeDesired is immutable for one Runtime owner lifetime. It is volatile:
// a successor must inventory and fence again before admitting any native launch.
type SystemdScopeDesired struct {
	StateDir                       string
	SandboxUID                     string
	Owner                          SystemdOwner
	Slice                          string
	AgentUID, AgentGID             uint32
	BlockDevices                   []string
	AggregateLimits, ProcessLimits ResourceLimits
}

func (SystemdScopeDesired) resourceType() resource.Type { return SystemdScopeDesiredType }
func (value SystemdScopeDesired) DeepCopy() SystemdScopeDesired {
	value.BlockDevices = append([]string(nil), value.BlockDevices...)
	return value
}

type SystemdScopeStatus struct {
	Ready bool
	Error string
}

func (SystemdScopeStatus) resourceType() resource.Type        { return SystemdScopeStatusType }
func (value SystemdScopeStatus) DeepCopy() SystemdScopeStatus { return value }

// Ready is a one-way latch. Reconciliation after readiness must never inventory
// again: the same namespace now also contains this owner's live processes.
type systemdScopeEntry struct {
	ready     *atomic.Bool
	reconcile func(context.Context, SystemdScopeDesired) error
}

// DeclareSystemdScope publishes intent without touching systemd or creating the
// invocation marker. The returned factory cannot launch before reconciliation.
func (h *Host) DeclareSystemdScope(ctx context.Context, launcher *SystemdLauncher, stateDir, sandboxUID string, owner SystemdOwner) (*Supervisor, error) {
	if launcher == nil {
		return nil, errors.New("VM process launcher is required")
	}
	configured := *launcher
	configured.BlockDevices = append([]string(nil), launcher.BlockDevices...)
	if err := configured.configureScope(stateDir, sandboxUID, owner); err != nil {
		return nil, err
	}
	configured.launchReady = &atomic.Bool{}
	desired := SystemdScopeDesired{
		StateDir: stateDir, SandboxUID: sandboxUID, Owner: owner, Slice: configured.Slice,
		AgentUID: configured.AgentUID, AgentGID: configured.AgentGID,
		BlockDevices: configured.BlockDevices, AggregateLimits: configured.AggregateLimits, ProcessLimits: configured.ProcessLimits,
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, exists := h.systemdScopes[owner.ScopeID]; exists {
		return nil, fmt.Errorf("systemd scope %q already declared", owner.ScopeID)
	}
	h.systemdScopes[owner.ScopeID] = &systemdScopeEntry{ready: configured.launchReady, reconcile: reconcileSystemdScope}
	if err := h.state.Create(ctx, resourceFor(h.namespace, string(owner.ScopeID), desired), state.WithCreateOwner(hostOwner)); err != nil {
		delete(h.systemdScopes, owner.ScopeID)
		return nil, err
	}
	supervisor := configured.newSupervisor()
	supervisor.systemdScope = owner.ScopeID
	return supervisor, nil
}

func (h *Host) SystemdScopeController() controller.QController {
	return &systemdScopeController{host: h}
}

func ReadSystemdScopeStatus(ctx context.Context, reader controller.Reader, namespace resource.Namespace, id SystemdScopeID) (SystemdScopeStatus, bool, error) {
	return readResource[SystemdScopeStatus](ctx, reader, namespace, string(id))
}

type systemdScopeController struct{ host *Host }

func (*systemdScopeController) Name() string { return "systemd-scope" }
func (c *systemdScopeController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs:      []controller.Input{{Namespace: c.host.namespace, Type: SystemdScopeDesiredType, Kind: controller.InputQPrimary}},
		Outputs:     []controller.Output{{Type: SystemdScopeStatusType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*systemdScopeController) MapInput(context.Context, *zap.Logger, controller.QRuntime, controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	return nil, nil
}

func (c *systemdScopeController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	desired, found, err := readResource[SystemdScopeDesired](ctx, r, c.host.namespace, ptr.ID())
	if err != nil {
		return err
	}
	if !found {
		return nil
	}
	if string(desired.Owner.ScopeID) != ptr.ID() {
		return errors.New("systemd scope identity changed")
	}
	c.host.mu.Lock()
	entry := c.host.systemdScopes[desired.Owner.ScopeID]
	c.host.mu.Unlock()
	if entry == nil {
		return errors.New("systemd scope has no registered configuration")
	}
	if entry.ready.Load() {
		return writeResource(ctx, r, c.host.namespace, ptr.ID(), SystemdScopeStatus{Ready: true})
	}
	if err := entry.reconcile(ctx, desired); err != nil {
		if writeErr := writeResource(ctx, r, c.host.namespace, ptr.ID(), SystemdScopeStatus{Error: err.Error()}); writeErr != nil {
			return errors.Join(err, writeErr)
		}
		return err
	}
	entry.ready.Store(true)
	return writeResource(ctx, r, c.host.namespace, ptr.ID(), SystemdScopeStatus{Ready: true})
}

func reconcileSystemdScope(ctx context.Context, desired SystemdScopeDesired) error {
	launcher := &SystemdLauncher{
		Slice: desired.Slice, AgentUID: desired.AgentUID, AgentGID: desired.AgentGID,
		BlockDevices: desired.BlockDevices, AggregateLimits: desired.AggregateLimits, ProcessLimits: desired.ProcessLimits,
	}
	if err := launcher.configureScope(desired.StateDir, desired.SandboxUID, desired.Owner); err != nil {
		return err
	}
	return launcher.reconcileScope(ctx, desired.Owner)
}
