package runtimecredentials

import (
	"context"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/identity/credentials/requester"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
)

// These VM annotations retain the accepted incarnation independently of Sandbox status.
const (
	VMIUIDAnnotation     = "agentplane.allegedly.works/environment-runtime-credential-vmi-uid"
	LastVMIUIDAnnotation = "agentplane.allegedly.works/environment-runtime-credential-last-vmi-uid"
)

// Workload is the controller's observed children. A standalone VMI may still be stopping.
type Workload struct {
	Pod      *corev1.Pod
	VM       *kubevirtv1.VirtualMachine
	VMI      *kubevirtv1.VirtualMachineInstance
	Launcher *corev1.Pod
}

// Binding is the incarnation the Sandbox accepted, or none.
func Binding(sandbox *sandboxapi.Sandbox) sandboxapi.RunnerCredential {
	if binding := sandbox.Status.RunnerCredential; binding != nil {
		return *binding
	}
	return sandboxapi.RunnerCredential{}
}

// Credential decisions may request WorkloadCredentials, but never mutate a child or Sandbox.
// The unexported method closes the set of outcomes consumed by the controller.
type Outcome interface{ isOutcome() }

type (
	KeepPublished struct{ Gate *Gate }
	Pending       struct{ Gate Gate }
	AwaitVMI      struct{}
	Revoke        struct{}
	RecordStart   struct {
		Issued requester.Issued
		Kind   sandboxapi.RunnerCredentialKind
	}
)

type (
	AcceptPod    struct{ Issued requester.Issued }
	AcceptVMI    struct{ Issued requester.Issued }
	RotateVMI    struct{ Message string }
	HaltMismatch struct{ Message string }
	ReplacePod   struct{ Message string }
)

func (KeepPublished) isOutcome() {}
func (Pending) isOutcome()       {}
func (AwaitVMI) isOutcome()      {}
func (Revoke) isOutcome()        {}
func (RecordStart) isOutcome()   {}
func (AcceptPod) isOutcome()     {}
func (AcceptVMI) isOutcome()     {}
func (RotateVMI) isOutcome()     {}
func (HaltMismatch) isOutcome()  {}
func (ReplacePod) isOutcome()    {}

// Decide requests credentials and returns the child/status action for the controller. Template is
// required only when there is no child and needsRuntime is true. Objects are never mutated.
func (r Requests) Decide(ctx context.Context, sandbox *sandboxapi.Sandbox, w Workload, needsRuntime bool, template *sandboxapi.SandboxTemplate) (Outcome, error) {
	switch {
	case w.Pod != nil:
		return r.ensurePod(ctx, sandbox, w)
	case w.VM != nil:
		return r.ensureVM(ctx, sandbox, w, needsRuntime)
	case w.VMI != nil:
		return KeepPublished{Gate: &Gate{Reason: "VMIStopping", Message: fmt.Sprintf("VMI %s is still stopping", w.VMI.Name)}}, nil
	case !needsRuntime:
		return Revoke{}, nil
	}
	kind := sandboxapi.RunnerCredentialKindPod
	if template.Spec.VM != nil {
		kind = sandboxapi.RunnerCredentialKindVM
	}
	issued, pending, err := r.RequestStart(ctx, sandbox)
	if err != nil {
		return nil, err
	}
	if pending != nil {
		return Pending{Gate: *pending}, nil
	}
	return RecordStart{Issued: issued, Kind: kind}, nil
}

func (r Requests) ensurePod(ctx context.Context, sandbox *sandboxapi.Sandbox, w Workload) (Outcome, error) {
	pod := w.Pod
	id := pod.Labels[sandboxapi.EnvironmentRuntimeCredentialMetadataKey]
	if !ValidID(id) {
		// Only the exact previously published legacy incarnation can retain its route.
		return KeepPublished{Gate: &Gate{Reason: "RunnerCredentialMissing", Message: fmt.Sprintf("Pod %s has no controller-issued Environment Runtime credential", pod.Name)}}, nil
	}
	if pod.UID == "" {
		return Pending{Gate: *credentialPending("RunnerCredentialPending", fmt.Sprintf("Pod %s has no assigned UID yet", pod.Name))}, nil
	}
	bound := Binding(sandbox)
	if bound.PodUID != "" && bound.PodUID != string(pod.UID) {
		return ReplacePod{Message: fmt.Sprintf("Pod %s is not the incarnation this Sandbox accepted", pod.Name)}, nil
	}
	issued, changed, pending, err := r.RequestIncarnation(ctx, sandbox, requester.Pod{Name: pod.Name, UID: pod.UID})
	if err != nil {
		return nil, err
	}
	if changed {
		return ReplacePod{Message: fmt.Sprintf("Pod %s has a credential subject its Sandbox no longer accepts", pod.Name)}, nil
	}
	if pending != nil {
		return KeepPublished{Gate: pending}, nil
	}
	if issued.ID != id || !issued.Accepted {
		return ReplacePod{Message: fmt.Sprintf("Pod %s was started with a credential its Sandbox no longer issues", pod.Name)}, nil
	}
	return AcceptPod{Issued: issued}, nil
}

// The VM annotations fence the accepted VMI independently of Sandbox status publication.
func (r Requests) ensureVM(ctx context.Context, sandbox *sandboxapi.Sandbox, w Workload, needsRuntime bool) (Outcome, error) {
	vm := w.VM
	if !vm.DeletionTimestamp.IsZero() {
		return KeepPublished{}, nil
	}
	id := vm.Annotations[sandboxapi.EnvironmentRuntimeCredentialMetadataKey]
	acceptedVMI := vm.Annotations[VMIUIDAnnotation]
	if w.VMI == nil && !VMRunning(vm) {
		if !needsRuntime {
			return Revoke{}, nil
		}
		issued, pending, err := r.RequestStart(ctx, sandbox)
		if err != nil {
			return nil, err
		}
		if pending != nil {
			return Pending{Gate: *pending}, nil
		}
		return RecordStart{Issued: issued, Kind: sandboxapi.RunnerCredentialKindVM}, nil
	}
	if !ValidID(id) {
		return KeepPublished{Gate: &Gate{Reason: "RunnerCredentialRequired", Message: "Environment Runtime mTLS credentials are installed after the legacy VM stops"}}, nil
	}
	vmi := w.VMI
	if vmi == nil {
		return AwaitVMI{}, nil
	}
	if vmi.Labels[sandboxapi.EnvironmentRuntimeCredentialMetadataKey] != id {
		return HaltMismatch{Message: fmt.Sprintf("VMI %s does not carry the VM's active Environment Runtime credential", vmi.Name)}, nil
	}
	if acceptedVMI != "" && acceptedVMI != string(vmi.UID) {
		return RotateVMI{Message: fmt.Sprintf("VMI %s was replaced; its credential is being rotated", vmi.Name)}, nil
	}
	binding := requester.VMI{UID: vmi.UID}
	if w.Launcher != nil {
		binding.Launcher = &requester.Pod{Name: w.Launcher.Name, UID: w.Launcher.UID}
	}
	issued, changed, pending, err := r.RequestIncarnation(ctx, sandbox, binding)
	if err != nil {
		return nil, err
	}
	if changed {
		return HaltMismatch{Message: fmt.Sprintf("VMI %s has a credential subject its Sandbox no longer accepts", vmi.Name)}, nil
	}
	if pending != nil {
		if acceptedVMI == string(vmi.UID) {
			return KeepPublished{Gate: pending}, nil
		}
		return Pending{Gate: *pending}, nil
	}
	if issued.ID != id || !issued.Accepted {
		return HaltMismatch{Message: fmt.Sprintf("VMI %s was started with a credential its Sandbox no longer issues", vmi.Name)}, nil
	}
	return AcceptVMI{Issued: issued}, nil
}

func ValidID(id string) bool {
	return len(id) == 32 && strings.Trim(id, "0123456789abcdef") == ""
}

// VMRunning reports the run strategy managed by the controller.
func VMRunning(vm *kubevirtv1.VirtualMachine) bool {
	return vm != nil && vm.Spec.RunStrategy != nil && *vm.Spec.RunStrategy == kubevirtv1.RunStrategyAlways
}
