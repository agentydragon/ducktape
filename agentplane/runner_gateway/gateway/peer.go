package gateway

import (
	"context"
	"net"
	"strconv"

	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type runnerDestination struct {
	identity sandboxreader.Identity
	podUID   string
	peerURI  string
	target   string
}

// runnerDestination derives identity and address from the active workload, never a caller's target.
func (g *Gateway) runnerDestination(ctx context.Context, sandbox *sandboxapi.Sandbox) (runnerDestination, error) {
	ref := sandbox.Status.Incarnation
	if ref == nil {
		return runnerDestination{}, status.Error(codes.Unavailable, "Sandbox has no runner incarnation")
	}
	pod, err := g.sandboxes.core.Pods(sandbox.Namespace).Get(ctx, ref.Name, metav1.GetOptions{})
	if err != nil {
		return runnerDestination{}, unavailable(err)
	}
	view := routingView(sandboxreader.View{Identity: sandboxreader.Identity{
		Namespace: sandbox.Namespace, Name: sandbox.Name, UID: string(sandbox.UID),
	}, Resource: sandbox})
	if !podIsReadyForSandbox(pod, view) || pod.Status.PodIP == "" {
		return runnerDestination{}, status.Error(codes.Unavailable, "runner Pod is unavailable")
	}
	credentialID := pod.Labels[sandboxapi.RunnerCredentialLabel]
	owner := metav1.GetControllerOf(pod)
	if owner.APIVersion == sandboxVMIAPIVer && owner.Kind == sandboxVMIKind {
		var vmi kubevirtv1.VirtualMachineInstance
		if err := g.objects.Get(ctx, client.ObjectKey{Namespace: pod.Namespace, Name: owner.Name}, &vmi); err != nil {
			return runnerDestination{}, unavailable(err)
		}
		if vmi.UID != owner.UID || !vmi.DeletionTimestamp.IsZero() || vmi.Labels[sandboxapi.SandboxLabel] != sandbox.Name {
			return runnerDestination{}, status.Error(codes.PermissionDenied, "runner VM incarnation changed")
		}
		vmOwner := metav1.GetControllerOf(&vmi)
		if vmOwner == nil || vmOwner.APIVersion != sandboxVMIAPIVer || vmOwner.Kind != "VirtualMachine" || vmOwner.Name != sandbox.Name {
			return runnerDestination{}, status.Error(codes.PermissionDenied, "runner VMI is not owned by its Sandbox VM")
		}
		var vm kubevirtv1.VirtualMachine
		if err := g.objects.Get(ctx, client.ObjectKey{Namespace: pod.Namespace, Name: vmOwner.Name}, &vm); err != nil {
			return runnerDestination{}, unavailable(err)
		}
		if vm.UID != vmOwner.UID || !metav1.IsControlledBy(&vm, sandbox) {
			return runnerDestination{}, status.Error(codes.PermissionDenied, "runner VM is not owned by its Sandbox")
		}
		credentialID = vmi.Labels[sandboxapi.RunnerCredentialLabel]
	}
	var uri string
	if g.config.RunnerSecurity.Mode == runner_transport.MutualTLS {
		var err error
		uri, err = runner_transport.RunnerURI(sandbox.Namespace, string(sandbox.UID), credentialID)
		if err != nil {
			return runnerDestination{}, err
		}
	}
	return runnerDestination{
		identity: view.identity, podUID: ref.UID, peerURI: uri,
		target: net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(sandboxapi.RunnerServicePort)),
	}, nil
}

// validateRunnerDestination rechecks the live workload independently of transport authentication.
func (g *Gateway) validateRunnerDestination(ctx context.Context, expected runnerDestination) error {
	if expected.identity.Namespace != g.config.Namespace {
		return status.Error(codes.PermissionDenied, "runner is outside the Gateway namespace")
	}
	var sandbox sandboxapi.Sandbox
	if err := g.objects.Get(ctx, client.ObjectKey{Namespace: expected.identity.Namespace, Name: expected.identity.Name}, &sandbox); err != nil {
		return unavailable(err)
	}
	if string(sandbox.UID) != expected.identity.UID {
		return status.Error(codes.PermissionDenied, "runner Sandbox was replaced")
	}
	current, err := g.runnerDestination(ctx, &sandbox)
	if err != nil {
		return err
	}
	if current.identity != expected.identity || current.podUID != expected.podUID || current.peerURI != expected.peerURI {
		return status.Error(codes.PermissionDenied, "runner incarnation or credential changed")
	}
	return nil
}
