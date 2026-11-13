package sandboxcontroller

import (
	"context"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"

	corev1 "k8s.io/api/core/v1"
)

// ensureControlService keeps the headless, selector-less Service whose EndpointSlice the Gateway owns.
func (r *Reconciler) ensureControlService(ctx context.Context, sandbox *sandboxapi.Sandbox) (*unready, error) {
	return r.applyOwned(ctx, sandbox, map[string]any{
		"apiVersion": "v1", "kind": "Service",
		"metadata": map[string]any{
			"name": sandbox.Name + sandboxapi.ControlServiceSuffix, "namespace": sandbox.Namespace,
			"labels": map[string]string{sandboxapi.SandboxLabel: sandbox.Name},
		},
		"spec": map[string]any{
			"clusterIP": corev1.ClusterIPNone, "selector": nil,
			"ports": []any{map[string]any{
				"name": sandboxapi.ControlServicePortName, "port": sandboxapi.ControlServicePort,
				"protocol": "TCP", "targetPort": sandboxapi.ControlServicePort,
			}},
		},
	}, "ServiceConflict")
}
