package sandboxcontroller

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strconv"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/client-go/kubernetes"
)

const (
	EnvironmentBackendLabel           = "agentplane.allegedly.works/environment-backend"
	FirecrackerRunningAnnotation      = "agentplane.allegedly.works/firecracker-running"
	FirecrackerSuspendEpochAnnotation = "agentplane.allegedly.works/firecracker-suspend-epoch"
	FirecrackerIntentMountPath        = "/run/agentplane/firecracker-intent"
	FirecrackerStateMountPath         = "/var/lib/agentplane/state"
	FirecrackerWorkspaceMountPath     = "/var/lib/agentplane/workspace"
	FirecrackerWorkspaceDevicePath    = "/dev/agentplane-workspace"
	FirecrackerBootMountPath          = "/images/boot"
	FirecrackerRunnerMountPath        = "/images/runner"
	firecrackerIntentVolume           = "agentplane-firecracker-intent"
	firecrackerBootVolume             = "agentplane-firecracker-boot"
	firecrackerRunnerVolume           = "agentplane-firecracker-runner"
	firecrackerStatusPort             = "9092"
	firecrackerStatusReadTimeout      = 5 * time.Second
)

type FirecrackerState struct {
	PodUID string `json:"pod_uid"`
	Phase  string `json:"phase"`
	Error  string `json:"error,omitempty"`
}

type FirecrackerStatusReader interface {
	Read(context.Context, *corev1.Pod) (FirecrackerState, error)
}

// PodProxyFirecrackerStatusReader only reads the host supervisor through the authenticated API server.
// It has no route to the Firecracker API socket or a mutating supervisor endpoint.
type PodProxyFirecrackerStatusReader struct{ Client kubernetes.Interface }

func (r PodProxyFirecrackerStatusReader) Read(ctx context.Context, pod *corev1.Pod) (FirecrackerState, error) {
	var state FirecrackerState
	readCtx, cancel := context.WithTimeout(ctx, firecrackerStatusReadTimeout)
	defer cancel()
	data, err := r.Client.CoreV1().RESTClient().Get().Namespace(pod.Namespace).
		Resource("pods").Name(pod.Name + ":" + firecrackerStatusPort).SubResource("proxy").Suffix("state").DoRaw(readCtx)
	if err != nil {
		return state, err
	}
	if err := json.Unmarshal(data, &state); err != nil {
		return state, fmt.Errorf("decode Firecracker supervisor status: %w", err)
	}
	return state, nil
}

func isFirecrackerPod(pod *corev1.Pod) bool {
	return pod != nil && pod.Labels[EnvironmentBackendLabel] == "firecracker"
}

// A restored VMM can run before the Gateway has accepted its new WatchState connection.
// Do not spend the persisted Quiesce attempt on the pre-checkpoint connection or a pending claim.
func (r *Reconciler) firecrackerQuiesceReady(ctx context.Context, sandbox *sandboxapi.Sandbox, pod *corev1.Pod) (*unready, error) {
	lease, err := r.runnerLease(ctx, sandbox)
	if err != nil {
		return nil, err
	}
	pending := &unready{reason: "Quiescing", stage: sandboxapi.StopStageQuiescing, message: "waiting for the restored Runtime's verified Gateway connection", retryAfter: 2 * time.Second}
	if value := pod.Annotations[FirecrackerSuspendEpochAnnotation]; value != "" {
		before, err := strconv.ParseInt(value, 10, 32)
		if err != nil || before < 0 || leaseTransitions(&lease) <= int32(before) {
			return pending, nil
		}
	}
	if lease.Annotations[sandboxlease.PodUIDAnnotation] != string(pod.UID) ||
		lease.Annotations[sandboxlease.EnvironmentRuntimeBootIDAnnotation] == "" || !connectionLive(&lease, r.Now()) {
		return pending, nil
	}
	return nil, nil
}

func withFirecracker(sandbox *sandboxapi.Sandbox, template *sandboxapi.SandboxTemplate, spec *corev1.PodSpec) *unready {
	f := template.Spec.Firecracker
	if f.BootImage == "" || f.RunnerImage == "" || f.VCPUCount < 1 || f.VCPUCount > 32 || f.MemoryMiB < 128 {
		return &unready{reason: "TemplateInvalid", message: "Firecracker images, vcpuCount and memoryMiB are required"}
	}
	i := slices.IndexFunc(spec.Containers, func(c corev1.Container) bool { return c.Name == EnvironmentRuntimeContainer })
	if i < 0 {
		return &unready{reason: "TemplateInvalid", message: "Firecracker launcher needs an environment-runtime container"}
	}
	for _, name := range []string{firecrackerIntentVolume, firecrackerBootVolume, firecrackerRunnerVolume} {
		if slices.ContainsFunc(spec.Volumes, func(v corev1.Volume) bool { return v.Name == name }) {
			return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("Firecracker template defines managed volume %s", name)}
		}
	}
	for _, path := range []string{FirecrackerIntentMountPath, FirecrackerStateMountPath, FirecrackerBootMountPath, FirecrackerRunnerMountPath, FirecrackerWorkspaceMountPath} {
		if slices.ContainsFunc(spec.Containers[i].VolumeMounts, func(m corev1.VolumeMount) bool { return m.MountPath == path }) {
			return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("Firecracker template defines managed mount %s", path)}
		}
	}
	if slices.ContainsFunc(spec.Containers[i].VolumeDevices, func(d corev1.VolumeDevice) bool { return d.DevicePath == FirecrackerWorkspaceDevicePath }) {
		return &unready{reason: "TemplateInvalid", message: "Firecracker template defines managed workspace device"}
	}
	spec.Volumes = append(
		spec.Volumes,
		corev1.Volume{Name: firecrackerIntentVolume, VolumeSource: corev1.VolumeSource{DownwardAPI: &corev1.DownwardAPIVolumeSource{Items: []corev1.DownwardAPIVolumeFile{{Path: "running", FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.annotations['" + FirecrackerRunningAnnotation + "']"}}}}}},
		corev1.Volume{Name: firecrackerBootVolume, VolumeSource: corev1.VolumeSource{Image: &corev1.ImageVolumeSource{Reference: f.BootImage, PullPolicy: corev1.PullIfNotPresent}}},
		corev1.Volume{Name: firecrackerRunnerVolume, VolumeSource: corev1.VolumeSource{Image: &corev1.ImageVolumeSource{Reference: f.RunnerImage, PullPolicy: corev1.PullIfNotPresent}}},
	)
	c := &spec.Containers[i]
	c.VolumeMounts = append(
		c.VolumeMounts,
		corev1.VolumeMount{Name: firecrackerIntentVolume, MountPath: FirecrackerIntentMountPath, ReadOnly: true},
		corev1.VolumeMount{Name: firecrackerBootVolume, MountPath: FirecrackerBootMountPath, ReadOnly: true},
		corev1.VolumeMount{Name: firecrackerRunnerVolume, MountPath: FirecrackerRunnerMountPath, ReadOnly: true},
		corev1.VolumeMount{Name: template.Spec.StateVolume.Name, MountPath: FirecrackerStateMountPath},
	)
	if workspace := template.Spec.WorkspaceVolume; workspace != nil {
		if workspace.Layout.Type == sandboxapi.BlockFilesystem {
			c.VolumeDevices = append(c.VolumeDevices, corev1.VolumeDevice{Name: workspace.Name, DevicePath: FirecrackerWorkspaceDevicePath})
		} else {
			c.VolumeMounts = append(c.VolumeMounts, corev1.VolumeMount{Name: workspace.Name, MountPath: FirecrackerWorkspaceMountPath})
		}
	}
	c.Env = append(
		c.Env,
		corev1.EnvVar{Name: "POD_UID", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.uid"}}},
		corev1.EnvVar{Name: "SANDBOX_UID", Value: string(sandbox.UID)},
		corev1.EnvVar{Name: "FIRECRACKER_VCPU_COUNT", Value: strconv.FormatInt(int64(f.VCPUCount), 10)},
		corev1.EnvVar{Name: "FIRECRACKER_MEMORY_MIB", Value: strconv.FormatInt(f.MemoryMiB, 10)},
		corev1.EnvVar{Name: "FIRECRACKER_BOOT_IMAGE", Value: f.BootImage},
		corev1.EnvVar{Name: "FIRECRACKER_RUNNER_IMAGE", Value: f.RunnerImage},
	)
	if template.Spec.WorkspaceVolume != nil {
		c.Env = append(c.Env, corev1.EnvVar{Name: "WORKSPACE_KIND", Value: string(template.Spec.WorkspaceVolume.Layout.Type)})
	}
	return nil
}

// convergeFirecracker keeps the same Pod and credential identity across suspend and resume.
// Supervisor observations never authorize a cold boot or Pod replacement.
func (r *Reconciler) convergeFirecracker(ctx context.Context, sandbox *sandboxapi.Sandbox, pod *corev1.Pod, running bool) (*unready, error) {
	if sandbox.DeletionTimestamp.IsZero() && firecrackerSuspending(sandbox) {
		return r.convergeFirecrackerSuspension(ctx, sandbox, pod)
	}
	return r.convergeFirecrackerIntent(ctx, sandbox, pod, running)
}

func (r *Reconciler) convergeFirecrackerIntent(ctx context.Context, sandbox *sandboxapi.Sandbox, pod *corev1.Pod, running bool) (*unready, error) {
	if pod.UID == "" {
		return &unready{reason: "FirecrackerPending", message: "launcher Pod has no UID", retryAfter: 2 * time.Second}, nil
	}
	if !pod.DeletionTimestamp.IsZero() || pod.Status.Phase == corev1.PodFailed || pod.Status.Phase == corev1.PodSucceeded {
		return &unready{reason: "FirecrackerLauncherLost", message: "launcher Pod ended; preserving checkpoint and credential identity"}, nil
	}
	want := strconv.FormatBool(running)
	if running && pod.Annotations[FirecrackerRunningAnnotation] == "false" {
		state, blocked := r.firecrackerState(ctx, pod)
		if blocked != nil {
			return blocked, nil
		}
		// DownwardAPI projections can coalesce intent changes. Complete the checkpoint
		// before resuming so the recorded connection epoch always requires a real reconnect.
		if state.Phase != "suspended" {
			return &unready{reason: "FirecrackerPending", message: "waiting for the pending suspension before resuming", retryAfter: 2 * time.Second}, nil
		}
	}
	if pod.Annotations[FirecrackerRunningAnnotation] != want || (!running && pod.Annotations[FirecrackerSuspendEpochAnnotation] == "") {
		updated := pod.DeepCopy()
		if updated.Annotations == nil {
			updated.Annotations = map[string]string{}
		}
		updated.Annotations[FirecrackerRunningAnnotation] = want
		if !running {
			lease, err := r.runnerLease(ctx, sandbox)
			if err != nil {
				return &unready{reason: "FirecrackerPending", message: "cannot record the connection epoch before suspension: " + err.Error(), retryAfter: 5 * time.Second}, nil
			}
			updated.Annotations[FirecrackerSuspendEpochAnnotation] = strconv.FormatInt(int64(leaseTransitions(&lease)), 10)
		}
		if err := r.Update(ctx, updated); err != nil {
			return nil, err
		}
		return &unready{reason: "FirecrackerPending", message: "waiting for launcher to observe desired state", retryAfter: 2 * time.Second}, nil
	}
	state, blocked := r.firecrackerState(ctx, pod)
	if blocked != nil {
		return blocked, nil
	}
	if running && state.Phase == "running" {
		return nil, nil
	}
	if !running && state.Phase == "suspended" {
		return &unready{reason: "Stopped", message: "Firecracker memory is checkpointed in the retained launcher Pod", stage: sandboxapi.StopStageStopped}, nil
	}
	return &unready{reason: "FirecrackerPending", message: fmt.Sprintf("launcher reports %s while desired running=%s", state.Phase, want), retryAfter: 2 * time.Second}, nil
}

func (r *Reconciler) firecrackerState(ctx context.Context, pod *corev1.Pod) (FirecrackerState, *unready) {
	if r.FirecrackerStatus == nil {
		return FirecrackerState{}, &unready{reason: "FirecrackerStatusUnavailable", message: "no supervisor status reader configured", retryAfter: 5 * time.Second}
	}
	state, err := r.FirecrackerStatus.Read(ctx, pod)
	if err != nil {
		return state, &unready{reason: "FirecrackerStatusUnavailable", message: err.Error(), retryAfter: 5 * time.Second}
	}
	if state.PodUID != string(pod.UID) {
		return state, &unready{reason: "FirecrackerStatusMismatch", message: "supervisor status has a different Pod UID", retryAfter: 5 * time.Second}
	}
	if state.Phase == "failed" {
		return state, &unready{reason: "FirecrackerFailed", message: state.Error, retryAfter: 5 * time.Second}
	}
	return state, nil
}
