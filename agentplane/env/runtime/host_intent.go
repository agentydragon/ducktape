package runtime

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"go.uber.org/zap"
)

const (
	hostIntentType       = "HostIntents.agentplane"
	hostIntentStatusType = "HostIntentStatuses.agentplane"
)

// Native handles stay on Runtime; this value is the immutable input to Host admission.
type hostIntent struct {
	RuntimeBootID   string
	HostBootID      string
	Generation      uint64
	SandboxUID      string
	IncarnationUID  string
	Mode            runtimepb.LifecycleIntent_Mode
	SealUID         string
	ExecutionFenced bool
}

func (v hostIntent) DeepCopy() hostIntent { return v }

type hostIntentStatus struct {
	Intent  hostIntent
	Applied bool
	Error   string
}

func (v hostIntentStatus) DeepCopy() hostIntentStatus { return v }

type hostIntentStatusExtension struct{}

func (hostIntentStatusExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: hostIntentStatusType, DefaultNamespace: lifecycle.Namespace}
}

func hostIntentStatusResource(v hostIntentStatus) *typed.Resource[hostIntentStatus, hostIntentStatusExtension] {
	return typed.NewResource[hostIntentStatus, hostIntentStatusExtension](resource.NewMetadata(lifecycle.Namespace, hostIntentStatusType, "host", resource.VersionUndefined), v)
}

type hostIntentExtension struct{}

func (hostIntentExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: hostIntentType, DefaultNamespace: lifecycle.Namespace}
}

func hostIntentResource(v hostIntent) *typed.Resource[hostIntent, hostIntentExtension] {
	return typed.NewResource[hostIntent, hostIntentExtension](resource.NewMetadata(lifecycle.Namespace, hostIntentType, "host", resource.VersionUndefined), v)
}

func intentValue(v *runtimepb.LifecycleIntent) hostIntent {
	return hostIntent{RuntimeBootID: v.Binding.RuntimeBootId, HostBootID: v.Binding.HostBootId, Generation: v.Binding.Generation, SandboxUID: v.SandboxUid, IncarnationUID: v.IncarnationUid, Mode: v.Mode, SealUID: v.SealUid, ExecutionFenced: v.ExecutionFenced}
}

func (v hostIntent) intent() *runtimepb.LifecycleIntent {
	return &runtimepb.LifecycleIntent{Binding: &runtimepb.Binding{RuntimeBootId: v.RuntimeBootID, HostBootId: v.HostBootID, Generation: v.Generation}, SandboxUid: v.SandboxUID, IncarnationUid: v.IncarnationUID, Mode: v.Mode, SealUid: v.SealUID, ExecutionFenced: v.ExecutionFenced}
}

// Application can wait for accepted Session setup. Its own controller leaves
// report observation and native drain controllers free to make progress.
type hostIntentController struct{ runtime *Runtime }

func (*hostIntentController) Name() string { return "environment-host-intent" }
func (*hostIntentController) Inputs() []controller.Input {
	return []controller.Input{{Namespace: lifecycle.Namespace, Type: hostIntentType, Kind: controller.InputWeak}}
}

func (*hostIntentController) Outputs() []controller.Output {
	return []controller.Output{{Type: hostIntentStatusType, Kind: controller.OutputExclusive}}
}

func (c *hostIntentController) Run(ctx context.Context, engine controller.Runtime, _ *zap.Logger) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-engine.EventCh():
		}
		desired, found, err := cosi.Read(ctx, engine, hostIntentResource(hostIntent{}))
		if err != nil {
			return err
		}
		current, currentFound, err := cosi.Read(ctx, engine, hostIntentStatusResource(hostIntentStatus{}))
		if err != nil {
			return err
		}
		if !found || currentFound && (current.Error != "" || current.Intent == desired) {
			continue
		}
		r := c.runtime
		r.mu.Lock()
		host := r.host
		if host == nil || !host.connected || host.bootID != desired.HostBootID {
			r.mu.Unlock()
			continue
		}
		peer := host.peer
		failed := r.failed
		r.mu.Unlock()
		if failed {
			continue
		}
		// Record before entering the effect. An Applying result is never retried,
		// including when the terminal publication fails after a native effect.
		result := hostIntentStatus{Intent: desired}
		if _, err := cosi.Write(ctx, engine, hostIntentStatusResource(result), result); err != nil {
			return err
		}
		applyErr := peer.ApplyIntent(ctx, desired.intent())
		if applyErr != nil {
			result.Error = applyErr.Error()
		} else {
			result.Applied = true
		}
		_, writeErr := cosi.Write(ctx, engine, hostIntentStatusResource(result), result)
		if applyErr != nil || writeErr != nil {
			r.fail(fmt.Errorf("apply Host lifecycle intent: %w", errors.Join(applyErr, writeErr)))
		}
		r.mu.Lock()
		r.changedLocked()
		r.mu.Unlock()
		engine.ResetRestartBackoff()
	}
}

// Caller holds Runtime.mu. Publishing never calls back into the Host mutex.
func (r *Runtime) publishHostIntentLocked() error {
	if r.lifecycleState == nil || r.intent == nil {
		return nil
	}
	value := intentValue(r.intent)
	return cosi.Publish(context.Background(), r.lifecycleState, hostIntentResource(value), value, lifecycle.EnvironmentOwner)
}
