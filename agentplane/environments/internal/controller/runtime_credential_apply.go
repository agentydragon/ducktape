package sandboxcontroller

import (
	"context"
	"fmt"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
)

// applyRuntimeCredential handles every variant of runtimeCredentialOutcome. Child writes precede
// the binding staged for updateStatus; a failed status write is recovered from the child next time.
func (r *Reconciler) applyRuntimeCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload, outcome runtimeCredentialOutcome) (workload, *unready, error) {
	switch outcome := outcome.(type) {
	case keepPublishedCredential:
		w.runtimeIncarnationPublished = previouslyPublishedRuntimeIncarnation(sandbox, w)
		return w, outcome.gate, nil
	case credentialPendingOutcome:
		return w, &outcome.gate, nil
	case awaitVMI:
		bound := runtimeCredentialBinding(sandbox)
		bound.Kind, bound.PodUID = sandboxapi.RunnerCredentialKindVM, ""
		bound.LastVMIUID = w.vm.Annotations[EnvironmentRuntimeCredentialLastVMIUIDAnnotation]
		w.credentialBinding = &bound
		return w, nil, nil
	case revokeRuntimeCredential:
		return w, nil, r.Credentials.Revoke(ctx, credentialOwner(sandbox))
	case recordStartCredential:
		if w.vm != nil {
			if err := r.recordVMStartCredential(ctx, w.vm, outcome.issued); err != nil {
				return w, nil, err
			}
		}
		bound := runtimeCredentialBinding(sandbox)
		bound.Kind, bound.PodUID = outcome.kind, ""
		if w.vm != nil {
			bound.LastVMIUID = w.vm.Annotations[EnvironmentRuntimeCredentialLastVMIUIDAnnotation]
		}
		w.credential, w.credentialBinding = outcome.issued, &bound
		return w, nil, nil
	case acceptPodCredential:
		bound := runtimeCredentialBinding(sandbox)
		bound.Kind, bound.PodUID = sandboxapi.RunnerCredentialKindPod, string(w.pod.UID)
		w.credentialBinding = &bound
		w.credential, w.runtimeCredentialAccepted, w.runtimeIncarnationPublished = outcome.issued, true, true
		return w, nil, nil
	case acceptVMICredential:
		if err := r.acceptVMICredential(ctx, w.vm, w.vmi); err != nil {
			return w, nil, err
		}
		bound := runtimeCredentialBinding(sandbox)
		bound.Kind, bound.LastVMIUID, bound.PodUID = sandboxapi.RunnerCredentialKindVM, string(w.vmi.UID), ""
		w.credentialBinding = &bound
		w.credential, w.runtimeCredentialAccepted, w.runtimeIncarnationPublished = outcome.issued, true, true
		return w, nil, nil
	case rotateVMICredential:
		if err := r.rotateVMICredential(ctx, w.vm); err != nil {
			return w, nil, err
		}
		// Unbinding revokes the recreated VMI's key only after the VM has been halted.
		if _, _, err := r.requestStartCredential(ctx, sandbox); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "RunnerCredentialMismatch", message: outcome.message}, nil
	case haltCredentialMismatch:
		if err := r.haltVM(ctx, w.vm); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "RunnerCredentialMismatch", message: outcome.message}, nil
	case replacePodCredential:
		return r.replaceStalePod(ctx, w, outcome.message)
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
