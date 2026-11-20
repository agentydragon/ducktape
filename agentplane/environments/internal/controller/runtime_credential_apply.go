package sandboxcontroller

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/environments/internal/runtimecredentials"
	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// runtimeCredentialRequests loads the live Identity and an uncached credential record. The
// decision package receives these objects and has no Kubernetes reader or writer of its own.
func (r *Reconciler) runtimeCredentialRequests(ctx context.Context, sandbox *sandboxapi.Sandbox) (runtimecredentials.Requests, error) {
	identity, err := r.readIdentity(ctx, sandbox)
	if err != nil {
		return runtimecredentials.Requests{}, err
	}
	var record workloadapi.WorkloadCredential
	if err := r.Objects.Get(ctx, client.ObjectKeyFromObject(sandbox), &record); err != nil {
		return runtimecredentials.Requests{Requester: r.Credentials, Identity: identity}, client.IgnoreNotFound(err)
	}
	return runtimecredentials.Requests{Requester: r.Credentials, Identity: identity, Record: &record}, nil
}

func credentialGate(gate *runtimecredentials.Gate) *unready {
	if gate == nil {
		return nil
	}
	return &unready{reason: gate.Reason, message: gate.Message, retryAfter: gate.RetryAfter}
}

func (r *Reconciler) ensureRuntimeCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload, needsRuntime bool) (workload, *unready, error) {
	var template *sandboxapi.SandboxTemplate
	if w.pod == nil && w.vm == nil && w.vmi == nil && needsRuntime {
		var blocked *unready
		var err error
		template, blocked, err = r.template(ctx, sandbox)
		if err != nil || blocked != nil {
			return w, blocked, err
		}
	}
	requests, err := r.runtimeCredentialRequests(ctx, sandbox)
	if err != nil {
		return w, nil, err
	}
	outcome, err := requests.Decide(ctx, sandbox, runtimecredentials.Workload{Pod: w.pod, VM: w.vm, VMI: w.vmi, Launcher: w.launcher}, needsRuntime, template)
	if err != nil {
		return w, nil, err
	}
	return r.applyRuntimeCredential(ctx, sandbox, w, outcome)
}

// applyRuntimeCredential handles every variant of runtimecredentials.Outcome. Child writes precede
// the binding staged for updateStatus; a failed status write is recovered from the child next time.
func (r *Reconciler) applyRuntimeCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload, outcome runtimecredentials.Outcome) (workload, *unready, error) {
	switch outcome := outcome.(type) {
	case runtimecredentials.KeepPublished:
		w.runtimeIncarnationPublished = previouslyPublishedRuntimeIncarnation(sandbox, w)
		return w, credentialGate(outcome.Gate), nil
	case runtimecredentials.Pending:
		return w, credentialGate(&outcome.Gate), nil
	case runtimecredentials.AwaitVMI:
		bound := runtimecredentials.Binding(sandbox)
		bound.Kind, bound.PodUID = sandboxapi.RunnerCredentialKindVM, ""
		bound.LastVMIUID = w.vm.Annotations[runtimecredentials.LastVMIUIDAnnotation]
		w.credentialBinding = &bound
		return w, nil, nil
	case runtimecredentials.Revoke:
		return w, nil, r.Credentials.Revoke(ctx, runtimecredentials.Owner(sandbox))
	case runtimecredentials.RecordStart:
		if w.vm != nil {
			if err := r.recordVMStartCredential(ctx, w.vm, outcome.Issued); err != nil {
				return w, nil, err
			}
		}
		bound := runtimecredentials.Binding(sandbox)
		bound.Kind, bound.PodUID = outcome.Kind, ""
		if w.vm != nil {
			bound.LastVMIUID = w.vm.Annotations[runtimecredentials.LastVMIUIDAnnotation]
		}
		w.credential, w.credentialBinding = outcome.Issued, &bound
		return w, nil, nil
	case runtimecredentials.AcceptPod:
		bound := runtimecredentials.Binding(sandbox)
		bound.Kind, bound.PodUID = sandboxapi.RunnerCredentialKindPod, string(w.pod.UID)
		w.credentialBinding = &bound
		w.credential, w.runtimeIncarnationPublished = outcome.Issued, true
		return w, nil, nil
	case runtimecredentials.AcceptVMI:
		if err := r.acceptVMICredential(ctx, w.vm, w.vmi); err != nil {
			return w, nil, err
		}
		bound := runtimecredentials.Binding(sandbox)
		bound.Kind, bound.LastVMIUID, bound.PodUID = sandboxapi.RunnerCredentialKindVM, string(w.vmi.UID), ""
		w.credentialBinding = &bound
		w.credential, w.runtimeIncarnationPublished = outcome.Issued, true
		return w, nil, nil
	case runtimecredentials.RotateVMI:
		if err := r.rotateVMICredential(ctx, w.vm); err != nil {
			return w, nil, err
		}
		// Unbinding revokes the recreated VMI's key only after the VM has been halted.
		requests, err := r.runtimeCredentialRequests(ctx, sandbox)
		if err != nil {
			return w, nil, err
		}
		if _, _, err := requests.RequestStart(ctx, sandbox); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "RunnerCredentialMismatch", message: outcome.Message}, nil
	case runtimecredentials.HaltMismatch:
		if err := r.haltVM(ctx, w.vm); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "RunnerCredentialMismatch", message: outcome.Message}, nil
	case runtimecredentials.ReplacePod:
		return r.replaceStalePod(ctx, w, outcome.Message)
	default:
		panic(fmt.Sprintf("unhandled runtime credential outcome %T", outcome))
	}
}

// A Firecracker launcher keeps its checkpoint for the operator to recover.
func (r *Reconciler) replaceStalePod(ctx context.Context, w workload, message string) (workload, *unready, error) {
	if isFirecrackerPod(w.pod) {
		return w, &unready{reason: "RunnerCredentialMismatch", message: message + "; preserving the Firecracker launcher and its checkpoint"}, nil
	}
	if err := r.deleteIfLive(ctx, w.pod); err != nil {
		return w, nil, err
	}
	return w, &unready{reason: "RunnerCredentialMismatch", message: message + " and is being replaced"}, nil
}
