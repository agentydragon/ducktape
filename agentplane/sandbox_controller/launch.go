package sandboxcontroller

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"slices"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	// RunnerContainer is the template container that gets the launch file. Must equal the runner
	// container's name in cluster/cdk8s/agentplane/runner_sandbox.py.
	RunnerContainer = "runner"
	// LaunchMountPath is the directory the runner container finds LaunchFile in. Must equal the
	// runner's --launch-file directory in cluster/cdk8s/agentplane/runner_sandbox.py.
	LaunchMountPath = "/run/agentplane/sandbox"
	// LaunchFile is the key of the Sandbox's launch ConfigMap that the runner reads
	// (agentplane/runner/sandbox_launch.py). A VM guest finds the same file on the disk of serial
	// LaunchDiskSerial, which agentplane/runner/vm.nix copies.
	LaunchFile       = "launch.json"
	LaunchDiskSerial = "agentplane-launch"

	launchVolume = "agentplane-launch"
)

// LaunchConfigMapName is the Sandbox's launch ConfigMap.
func LaunchConfigMapName(sandbox string) string { return sandbox + "-launch" }

// launch is what the runner needs to know of its Sandbox: who it is, for the destination its
// Sessions give the agent, and the bootstrap it runs once before its first Session.
type launch struct {
	Sandbox   launchSandbox `json:"sandbox"`
	Bootstrap string        `json:"bootstrap,omitempty"`
}

type launchSandbox struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	UID       string `json:"uid"`
}

// ensureLaunch keeps the Sandbox's launch ConfigMap. It returns why no Pod may start yet; a running
// Pod is never stopped for it. A changed bootstrap reaches the Sandbox's next incarnation only.
func (r *Reconciler) ensureLaunch(ctx context.Context, sandbox *Sandbox) (*unready, error) {
	content, err := json.Marshal(launch{
		Sandbox:   launchSandbox{Namespace: sandbox.Namespace, Name: sandbox.Name, UID: string(sandbox.UID)},
		Bootstrap: sandbox.Spec.Bootstrap,
	})
	if err != nil {
		return nil, err
	}
	desired := map[string]string{LaunchFile: string(content)}
	configMap := &corev1.ConfigMap{ObjectMeta: owned(sandbox)}
	configMap.Name = LaunchConfigMapName(sandbox.Name)
	if err := r.Get(ctx, client.ObjectKeyFromObject(configMap), configMap); err != nil {
		if !apierrors.IsNotFound(err) {
			return nil, err
		}
		configMap.Data = desired
		return nil, r.createOwned(ctx, sandbox, configMap)
	}
	if !metav1.IsControlledBy(configMap, sandbox) {
		return &unready{"ConfigMapConflict", fmt.Sprintf("ConfigMap %s exists and is not this Sandbox's", configMap.Name)}, nil
	}
	if maps.Equal(configMap.Data, desired) {
		return nil, nil
	}
	configMap.Data = desired
	return nil, r.Update(ctx, configMap)
}

// withLaunch mounts the Sandbox's launch ConfigMap into the template's runner container.
func withLaunch(sandbox *Sandbox, template *SandboxTemplate, spec *corev1.PodSpec) *unready {
	i := slices.IndexFunc(spec.Containers, func(c corev1.Container) bool { return c.Name == RunnerContainer })
	if i < 0 {
		return &unready{"TemplateInvalid", fmt.Sprintf("SandboxTemplate %s has no %s container", template.Name, RunnerContainer)}
	}
	if slices.ContainsFunc(spec.Volumes, func(v corev1.Volume) bool { return v.Name == launchVolume }) {
		return &unready{"TemplateInvalid", fmt.Sprintf("SandboxTemplate %s defines the launch volume %s itself", template.Name, launchVolume)}
	}
	spec.Volumes = append(spec.Volumes, corev1.Volume{
		Name: launchVolume,
		VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: LaunchConfigMapName(sandbox.Name)},
		}},
	})
	spec.Containers[i].VolumeMounts = append(spec.Containers[i].VolumeMounts, corev1.VolumeMount{
		Name: launchVolume, MountPath: LaunchMountPath, ReadOnly: true,
	})
	return nil
}

// withLaunchDisk attaches the Sandbox's launch ConfigMap to the VM as a disk the guest finds by
// serial.
func withLaunchDisk(sandbox *Sandbox, template *SandboxTemplate, instance *kubevirtv1.VirtualMachineInstanceSpec) *unready {
	if slices.ContainsFunc(instance.Volumes, func(v kubevirtv1.Volume) bool { return v.Name == launchVolume }) ||
		slices.ContainsFunc(instance.Domain.Devices.Disks, func(d kubevirtv1.Disk) bool { return d.Name == launchVolume || d.Serial == LaunchDiskSerial }) {
		return &unready{"TemplateInvalid", fmt.Sprintf("SandboxTemplate %s defines the launch volume %s itself", template.Name, launchVolume)}
	}
	instance.Volumes = append(instance.Volumes, kubevirtv1.Volume{
		Name: launchVolume,
		VolumeSource: kubevirtv1.VolumeSource{ConfigMap: &kubevirtv1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: LaunchConfigMapName(sandbox.Name)},
		}},
	})
	instance.Domain.Devices.Disks = append(instance.Domain.Devices.Disks, kubevirtv1.Disk{
		Name:       launchVolume,
		Serial:     LaunchDiskSerial,
		DiskDevice: kubevirtv1.DiskDevice{Disk: &kubevirtv1.DiskTarget{Bus: kubevirtv1.DiskBusVirtio}},
	})
	return nil
}
