package sandboxcontroller

import (
	"context"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/util/intstr"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// ensureControlService creates the Sandbox's control Service, so a caller resolves it before any
// runner has connected and finds no endpoints rather than a DNS failure. The Runner Gateway points
// its EndpointSlice at the runner's connection. A Service already owned by the Sandbox, as the
// Runner Gateway created them before, is kept.
func (r *Reconciler) ensureControlService(ctx context.Context, sandbox *sandboxapi.Sandbox) error {
	service := &corev1.Service{ObjectMeta: owned(sandbox)}
	service.Name = sandbox.Name + sandboxapi.ControlServiceSuffix
	err := r.Get(ctx, client.ObjectKeyFromObject(service), service)
	if err == nil || !apierrors.IsNotFound(err) {
		return err
	}
	// Headless and selector-less: DNS returns the Runner Gateway's EndpointSlice address.
	service.Spec = corev1.ServiceSpec{
		ClusterIP: corev1.ClusterIPNone,
		Ports:     []corev1.ServicePort{{Name: sandboxapi.ControlServicePortName, Port: sandboxapi.ControlServicePort, TargetPort: intstr.FromInt32(sandboxapi.ControlServicePort)}},
	}
	return r.createOwned(ctx, sandbox, service)
}
