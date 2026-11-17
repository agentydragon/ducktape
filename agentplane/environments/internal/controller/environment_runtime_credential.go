package sandboxcontroller

import (
	"fmt"
	"slices"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
)

const (
	EnvironmentRuntimeTLSVolumeName   = "agentplane-environment-runtime-tls"
	EnvironmentRuntimeTLSMountPath    = "/run/agentplane/environment-runtime-tls"
	EnvironmentRuntimeEgressSidecar   = "egress-sidecar"
	EnvironmentRuntimeTrustVolumeName = "agentplane-environment-runtime-trust"
	EnvironmentRuntimeTrustMountPath  = "/run/agentplane/environment-runtime-trust"
	EnvironmentRuntimeTrustBundleName = "cluster-internal-ca-bundle"
	EnvironmentRuntimeTrustBundleKey  = "ca-certificates.crt"

	// The workload CA alone, which verifies the Gateway's client SVID at the Environment Runtime's control
	// listener and, in the egress sidecar or launcher relay, the server SVIDs the Environment Runtime dials; the
	// cluster bundle above verifies only the Runtime's own chain. A Pod gets it as
	// EnvironmentRuntimeWorkloadTrustFile beside ca.crt, a VM as its own filesystem.
	EnvironmentRuntimeWorkloadTrustVolumeName = "runner-workload-trust"
	EnvironmentRuntimeWorkloadTrustBundleName = "agentplane-workload-ca-bundle"
	EnvironmentRuntimeWorkloadTrustFile       = "workload-ca.crt"
)

func withEnvironmentRuntimeTLS(template *sandboxapi.SandboxTemplate, spec *corev1.PodSpec, secretName string) *unready {
	i := slices.IndexFunc(spec.Containers, func(container corev1.Container) bool { return container.Name == EnvironmentRuntimeContainer })
	if i < 0 {
		return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s has no %s container", template.Name, EnvironmentRuntimeContainer)}
	}
	proxy := slices.IndexFunc(spec.InitContainers, func(container corev1.Container) bool { return container.Name == EnvironmentRuntimeEgressSidecar })
	proxyIsInit := proxy >= 0
	if proxy < 0 {
		proxy = slices.IndexFunc(spec.Containers, func(container corev1.Container) bool { return container.Name == EnvironmentRuntimeEgressSidecar })
	}
	reservedMount := func(container corev1.Container) bool {
		return slices.ContainsFunc(container.VolumeMounts, func(mount corev1.VolumeMount) bool {
			return mount.Name == EnvironmentRuntimeTLSVolumeName || mount.MountPath == EnvironmentRuntimeTLSMountPath
		})
	}
	// A template can claim the injected Secret by mounting its reserved volume name
	// without declaring the volume itself, so check every container before injection.
	if slices.ContainsFunc(spec.Volumes, func(volume corev1.Volume) bool { return volume.Name == EnvironmentRuntimeTLSVolumeName }) ||
		slices.ContainsFunc(spec.Containers, reservedMount) || slices.ContainsFunc(spec.InitContainers, reservedMount) {
		return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s defines the reserved Environment Runtime TLS volume or mount", template.Name)}
	}
	mode := int32(0440)
	spec.Volumes = append(spec.Volumes, corev1.Volume{
		Name: EnvironmentRuntimeTLSVolumeName,
		VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: &mode,
			Sources: []corev1.VolumeProjection{
				{Secret: &corev1.SecretProjection{
					LocalObjectReference: corev1.LocalObjectReference{Name: secretName},
					Items:                []corev1.KeyToPath{{Key: "tls.crt", Path: "tls.crt"}, {Key: "tls.key", Path: "tls.key"}},
				}},
				{ConfigMap: &corev1.ConfigMapProjection{
					LocalObjectReference: corev1.LocalObjectReference{Name: EnvironmentRuntimeTrustBundleName},
					Items:                []corev1.KeyToPath{{Key: EnvironmentRuntimeTrustBundleKey, Path: "ca.crt"}},
				}},
				{ConfigMap: &corev1.ConfigMapProjection{
					LocalObjectReference: corev1.LocalObjectReference{Name: EnvironmentRuntimeWorkloadTrustBundleName},
					Items:                []corev1.KeyToPath{{Key: EnvironmentRuntimeTrustBundleKey, Path: EnvironmentRuntimeWorkloadTrustFile}},
				}},
			},
		}},
	})
	spec.Containers[i].VolumeMounts = append(spec.Containers[i].VolumeMounts, corev1.VolumeMount{
		Name: EnvironmentRuntimeTLSVolumeName, MountPath: EnvironmentRuntimeTLSMountPath, ReadOnly: true,
	})
	if proxy >= 0 {
		proxyMount := corev1.VolumeMount{
			Name: EnvironmentRuntimeTLSVolumeName, MountPath: EnvironmentRuntimeTLSMountPath, ReadOnly: true,
		}
		if proxyIsInit {
			spec.InitContainers[proxy].VolumeMounts = append(spec.InitContainers[proxy].VolumeMounts, proxyMount)
		} else {
			spec.Containers[proxy].VolumeMounts = append(spec.Containers[proxy].VolumeMounts, proxyMount)
		}
	}
	return nil
}

func withEnvironmentRuntimeTLSDisk(template *sandboxapi.SandboxTemplate, instance *kubevirtv1.VirtualMachineInstanceSpec, secretName string) *unready {
	reserved := []string{EnvironmentRuntimeTLSVolumeName, EnvironmentRuntimeTrustVolumeName, EnvironmentRuntimeWorkloadTrustVolumeName}
	if slices.ContainsFunc(instance.Volumes, func(volume kubevirtv1.Volume) bool {
		return slices.Contains(reserved, volume.Name)
	}) || slices.ContainsFunc(instance.Domain.Devices.Filesystems, func(filesystem kubevirtv1.Filesystem) bool {
		return slices.Contains(reserved, filesystem.Name)
	}) {
		return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s defines the reserved Environment Runtime TLS volume", template.Name)}
	}
	instance.Volumes = append(instance.Volumes, kubevirtv1.Volume{
		Name: EnvironmentRuntimeTLSVolumeName,
		VolumeSource: kubevirtv1.VolumeSource{Secret: &kubevirtv1.SecretVolumeSource{
			SecretName: secretName,
		}},
	})
	instance.Volumes = append(instance.Volumes, kubevirtv1.Volume{
		Name: EnvironmentRuntimeTrustVolumeName,
		VolumeSource: kubevirtv1.VolumeSource{ConfigMap: &kubevirtv1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: EnvironmentRuntimeTrustBundleName},
		}},
	})
	instance.Domain.Devices.Filesystems = append(instance.Domain.Devices.Filesystems, kubevirtv1.Filesystem{
		Name: EnvironmentRuntimeTLSVolumeName, Virtiofs: &kubevirtv1.FilesystemVirtiofs{},
	})
	instance.Volumes = append(instance.Volumes, kubevirtv1.Volume{
		Name: EnvironmentRuntimeWorkloadTrustVolumeName,
		VolumeSource: kubevirtv1.VolumeSource{ConfigMap: &kubevirtv1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: EnvironmentRuntimeWorkloadTrustBundleName},
		}},
	})
	instance.Domain.Devices.Filesystems = append(instance.Domain.Devices.Filesystems, kubevirtv1.Filesystem{
		Name: EnvironmentRuntimeTrustVolumeName, Virtiofs: &kubevirtv1.FilesystemVirtiofs{},
	})
	instance.Domain.Devices.Filesystems = append(instance.Domain.Devices.Filesystems, kubevirtv1.Filesystem{
		Name: EnvironmentRuntimeWorkloadTrustVolumeName, Virtiofs: &kubevirtv1.FilesystemVirtiofs{},
	})
	return nil
}

func ensureAnnotations(meta metav1.Object) {
	if meta.GetAnnotations() == nil {
		meta.SetAnnotations(map[string]string{})
	}
}
