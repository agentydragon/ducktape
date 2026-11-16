package runtimeproxy

import (
	"context"
	"fmt"
	"net"
	"strconv"

	"github.com/agentydragon/ducktape/agentplane/identity/credentials/workloadid"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type runtimeDestination struct {
	identity *sandboxreader.Identity
	podUID   string
	peerURI  string
	target   string
}

// equal compares identity by value: each Kubernetes read produces a new protobuf message.
func (d runtimeDestination) equal(other runtimeDestination) bool {
	return proto.Equal(d.identity, other.identity) && d.podUID == other.podUID &&
		d.peerURI == other.peerURI && d.target == other.target
}

// runtimeDestination derives the Sandbox identity and address from its active Environment Runtime workload.
func (g *Server) runtimeDestination(ctx context.Context, sandbox *sandboxapi.Sandbox) (runtimeDestination, error) {
	ref := sandbox.Status.Incarnation
	if ref == nil {
		return runtimeDestination{}, status.Error(codes.Unavailable, "Sandbox has no Environment Runtime incarnation")
	}
	var pod corev1.Pod
	if err := g.readWorkload(ctx, sandbox.Namespace, ref.Name, &pod); err != nil {
		return runtimeDestination{}, err
	}
	view := routingView(sandboxreader.View{Identity: &sandboxreader.Identity{
		Namespace: sandbox.Namespace, Name: sandbox.Name, Uid: string(sandbox.UID),
	}, Resource: sandbox})
	if !podIsReadyForSandbox(&pod, view) || pod.Status.PodIP == "" {
		return runtimeDestination{}, status.Error(codes.Unavailable, "Environment Runtime Pod is unavailable")
	}
	credentialID := pod.Labels[sandboxapi.EnvironmentRuntimeCredentialMetadataKey]
	owner := metav1.GetControllerOf(&pod)
	if owner.APIVersion == sandboxVMIAPIVer && owner.Kind == sandboxVMIKind {
		var vmi kubevirtv1.VirtualMachineInstance
		if err := g.readWorkload(ctx, pod.Namespace, owner.Name, &vmi); err != nil {
			return runtimeDestination{}, err
		}
		if vmi.UID != owner.UID || !vmi.DeletionTimestamp.IsZero() || vmi.Labels[sandboxapi.SandboxLabel] != sandbox.Name {
			return runtimeDestination{}, status.Error(codes.PermissionDenied, "Sandbox workload VM incarnation changed")
		}
		vmOwner := metav1.GetControllerOf(&vmi)
		if vmOwner == nil || vmOwner.APIVersion != sandboxVMIAPIVer || vmOwner.Kind != "VirtualMachine" || vmOwner.Name != sandbox.Name {
			return runtimeDestination{}, status.Error(codes.PermissionDenied, "workload VMI is not owned by its Sandbox VM")
		}
		var vm kubevirtv1.VirtualMachine
		if err := g.readWorkload(ctx, pod.Namespace, vmOwner.Name, &vm); err != nil {
			return runtimeDestination{}, err
		}
		if vm.UID != vmOwner.UID || !metav1.IsControlledBy(&vm, sandbox) {
			return runtimeDestination{}, status.Error(codes.PermissionDenied, "workload VM is not owned by its Sandbox")
		}
		credentialID = vmi.Labels[sandboxapi.EnvironmentRuntimeCredentialMetadataKey]
	}
	var uri string
	if g.config.RunnerSecurity.Mode == peertls.MutualTLS {
		var err error
		uri, err = workloadid.ID{Namespace: sandbox.Namespace, Name: sandbox.Name, CredentialID: credentialID}.URI()
		if err != nil {
			return runtimeDestination{}, err
		}
	}
	return runtimeDestination{
		identity: view.identity, podUID: ref.UID, peerURI: uri,
		target: net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(sandboxapi.EnvironmentRuntimeServicePort)),
	}, nil
}

// readWorkload reads one object used to route to a Sandbox's Environment Runtime. A missing object
// means that route is unavailable; any other failure is retried.
func (g *Server) readWorkload(ctx context.Context, namespace, name string, object client.Object) error {
	err := g.objects.Get(ctx, client.ObjectKey{Namespace: namespace, Name: name}, object)
	if apierrors.IsNotFound(err) {
		return status.Errorf(codes.Unavailable, "Environment Runtime workload %T is gone", object)
	}
	if err != nil {
		return fmt.Errorf("read Environment Runtime workload %T: %w", object, err)
	}
	return nil
}

// validateRuntimeDestination rechecks the workload on every Environment Runtime heartbeat, independently of
// transport authentication. Cached reads reject replaced workloads locally; the Runtime checks
// Sandbox UID, incarnation and boot ID on every forwarded call, including from a stale replica.
func (g *Server) validateRuntimeDestination(ctx context.Context, expected runtimeDestination) error {
	if expected.identity.Namespace != g.config.Namespace {
		return status.Error(codes.PermissionDenied, "Environment Runtime is outside the Environments namespace")
	}
	var sandbox sandboxapi.Sandbox
	if err := g.readWorkload(ctx, expected.identity.Namespace, expected.identity.Name, &sandbox); err != nil {
		return err
	}
	if string(sandbox.UID) != expected.identity.Uid {
		return status.Error(codes.PermissionDenied, "Sandbox incarnation was replaced")
	}
	current, err := g.runtimeDestination(ctx, &sandbox)
	if err != nil {
		return err
	}
	if !proto.Equal(current.identity, expected.identity) || current.podUID != expected.podUID || current.peerURI != expected.peerURI {
		return status.Error(codes.PermissionDenied, "Environment Runtime incarnation or credential changed")
	}
	return nil
}
