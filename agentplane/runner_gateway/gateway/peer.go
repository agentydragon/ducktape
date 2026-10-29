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
	uri, err := runner_transport.RunnerURI(sandbox.Namespace, string(sandbox.UID), credentialID)
	if err != nil {
		return runnerDestination{}, err
	}
	return runnerDestination{
		identity: view.identity, podUID: ref.UID, peerURI: uri,
		target: net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(sandboxapi.RunnerServicePort)),
	}, nil
}

func (g *Gateway) certificatePeer(ctx context.Context, peerURI string) (sandboxreader.Identity, string, error) {
	namespace, sandboxUID, _, err := runner_transport.ParseRunnerURI(peerURI)
	if err != nil || namespace != g.config.Namespace {
		return sandboxreader.Identity{}, "", status.Error(codes.PermissionDenied, "certificate does not identify an admitted runner")
	}
	var sandboxes sandboxapi.SandboxList
	if err := g.objects.List(ctx, &sandboxes, client.InNamespace(namespace)); err != nil {
		return sandboxreader.Identity{}, "", unavailable(err)
	}
	for i := range sandboxes.Items {
		if string(sandboxes.Items[i].UID) != sandboxUID {
			continue
		}
		destination, err := g.runnerDestination(ctx, &sandboxes.Items[i])
		if err != nil {
			return sandboxreader.Identity{}, "", err
		}
		if destination.peerURI != peerURI {
			return sandboxreader.Identity{}, "", status.Error(codes.PermissionDenied, "runner certificate is stale")
		}
		return destination.identity, destination.podUID, nil
	}
	return sandboxreader.Identity{}, "", status.Error(codes.PermissionDenied, "runner Sandbox no longer exists")
}
