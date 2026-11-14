package sandboxcontroller

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	corev1ac "k8s.io/client-go/applyconfigurations/core/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
)

const (
	// EnvironmentRuntimeContainer is the template container that gets the launch file.
	EnvironmentRuntimeContainer = "environment-runtime"
	// LaunchMountPath is the directory the runner container finds LaunchFile in. Must equal the
	// runner's --launch-file directory in cluster/cdk8s/agentplane/environment_sandbox.py.
	LaunchMountPath = "/run/agentplane/sandbox"
	// LaunchFile is the key of the Sandbox's launch ConfigMap that the Go runner reads.
	// A VM guest finds the same file on the disk of serial
	// LaunchDiskSerial, mounted at the same LaunchMountPath by agentplane/env/images/vm.nix.
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
func (r *Reconciler) ensureLaunch(ctx context.Context, sandbox *sandboxapi.Sandbox) (*unready, error) {
	content, err := json.Marshal(launch{
		Sandbox:   launchSandbox{Namespace: sandbox.Namespace, Name: sandbox.Name, UID: string(sandbox.UID)},
		Bootstrap: sandbox.Spec.Bootstrap,
	})
	if err != nil {
		return nil, err
	}
	return r.applyOwned(ctx, sandbox,
		corev1ac.ConfigMap(LaunchConfigMapName(sandbox.Name), sandbox.Namespace).
			WithLabels(map[string]string{sandboxapi.SandboxLabel: sandbox.Name}).
			WithData(map[string]string{LaunchFile: string(content)}), "ConfigMapConflict")
}

// withLaunch mounts the Sandbox's launch ConfigMap into the template's runner container.
func withLaunch(sandbox *sandboxapi.Sandbox, template *sandboxapi.SandboxTemplate, spec *corev1.PodSpec) *unready {
	i := slices.IndexFunc(spec.Containers, func(c corev1.Container) bool { return c.Name == EnvironmentRuntimeContainer })
	if i < 0 {
		return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s has no %s container", template.Name, EnvironmentRuntimeContainer)}
	}
	if slices.ContainsFunc(spec.Volumes, func(v corev1.Volume) bool { return v.Name == launchVolume }) {
		return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s defines the launch volume %s itself", template.Name, launchVolume)}
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
func withLaunchDisk(sandbox *sandboxapi.Sandbox, template *sandboxapi.SandboxTemplate, instance *kubevirtv1.VirtualMachineInstanceSpec) *unready {
	if slices.ContainsFunc(instance.Volumes, func(v kubevirtv1.Volume) bool { return v.Name == launchVolume }) ||
		slices.ContainsFunc(instance.Domain.Devices.Disks, func(d kubevirtv1.Disk) bool { return d.Name == launchVolume || d.Serial == LaunchDiskSerial }) {
		return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s defines the launch volume %s itself", template.Name, launchVolume)}
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
