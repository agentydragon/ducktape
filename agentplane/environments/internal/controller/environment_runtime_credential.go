package sandboxcontroller

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/credentials/requester"
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

	// A VM's active credential and the VMI accepted for it. The Sandbox's binding is in
	// status.runnerCredential.
	EnvironmentRuntimeCredentialVMIUIDAnnotation     = "agentplane.allegedly.works/environment-runtime-credential-vmi-uid"
	EnvironmentRuntimeCredentialLastVMIUIDAnnotation = "agentplane.allegedly.works/environment-runtime-credential-last-vmi-uid"

	runtimeCredentialRetry = 5 * time.Second
)

// runtimeCredentialBinding is the incarnation the Sandbox accepted, or none.
func runtimeCredentialBinding(sandbox *sandboxapi.Sandbox) sandboxapi.RunnerCredential {
	if binding := sandbox.Status.RunnerCredential; binding != nil {
		return *binding
	}
	return sandboxapi.RunnerCredential{}
}

// credentialOwner is the Sandbox as its WorkloadCredential's owner: the record takes the Sandbox's
// name and goes with it.
func credentialOwner(sandbox *sandboxapi.Sandbox) requester.Owner {
	return requester.OwnedBy(sandbox, sandboxapi.GroupVersion.WithKind(sandboxapi.SandboxKind))
}

// requestStartCredential asks for the unbound credential the Sandbox's next incarnation starts
// with. A record naming another subject is revoked first, since a record's subject changes only
// with a new binding.
func (r *Reconciler) requestStartCredential(ctx context.Context, sandbox *sandboxapi.Sandbox) (requester.Issued, *unready, error) {
	subject, changed, blocked, err := r.credentialSubject(ctx, sandbox, false)
	if err != nil || blocked != nil {
		return requester.Issued{}, blocked, err
	}
	if changed {
		return requester.Issued{}, credentialPending("IdentityChanged", "the WorkloadCredential subject differs from the Sandbox's Identity and is being revoked"),
			r.Credentials.Revoke(ctx, credentialOwner(sandbox))
	}
	return r.requestCredential(ctx, sandbox, subject, nil)
}

// requestIncarnationCredential asks for the credential bound to the incarnation the Sandbox
// accepts. changed says the incarnation's recorded subject differs from the Sandbox's Identity,
// and must go instead.
func (r *Reconciler) requestIncarnationCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, binding requester.Binding) (issued requester.Issued, changed bool, pending *unready, err error) {
	subject, changed, _, err := r.credentialSubject(ctx, sandbox, true)
	if err != nil || changed {
		return requester.Issued{}, changed, nil, err
	}
	issued, pending, err = r.requestCredential(ctx, sandbox, subject, binding)
	return issued, false, pending, err
}

// requestCredential asks for the credential the Sandbox's incarnation runs with: binding is the one
// it accepts, or nil before one exists.
func (r *Reconciler) requestCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, subject requester.Identity, binding requester.Binding) (requester.Issued, *unready, error) {
	credential, err := r.Credentials.Ensure(ctx, requester.Request{
		Owner: credentialOwner(sandbox), Subject: subject, Binding: binding,
	})
	if err != nil {
		return requester.Issued{}, nil, err
	}
	switch credential := credential.(type) {
	case requester.Issued:
		return credential, nil, nil
	case requester.Pending:
		return requester.Issued{}, credentialPending(credential.Reason, credential.Message), nil
	default:
		return requester.Issued{}, nil, fmt.Errorf("unknown credential %T", credential)
	}
}

// Credential decisions may request WorkloadCredentials, but never mutate a child or Sandbox.
// The unexported method closes the set of outcomes consumed by applyRuntimeCredential.
type runtimeCredentialOutcome interface{ isRuntimeCredentialOutcome() }

type (
	keepPublishedCredential  struct{ gate *unready }
	credentialPendingOutcome struct{ gate unready }
	awaitVMI                 struct{}
	revokeRuntimeCredential  struct{}
	recordStartCredential    struct {
		issued requester.Issued
		kind   sandboxapi.RunnerCredentialKind
	}
)

type (
	acceptPodCredential    struct{ issued requester.Issued }
	acceptVMICredential    struct{ issued requester.Issued }
	rotateVMICredential    struct{ message string }
	haltCredentialMismatch struct{ message string }
	replacePodCredential   struct{ message string }
)

func (keepPublishedCredential) isRuntimeCredentialOutcome()  {}
func (credentialPendingOutcome) isRuntimeCredentialOutcome() {}
func (awaitVMI) isRuntimeCredentialOutcome()                 {}
func (revokeRuntimeCredential) isRuntimeCredentialOutcome()  {}
func (recordStartCredential) isRuntimeCredentialOutcome()    {}
func (acceptPodCredential) isRuntimeCredentialOutcome()      {}
func (acceptVMICredential) isRuntimeCredentialOutcome()      {}
func (rotateVMICredential) isRuntimeCredentialOutcome()      {}
func (haltCredentialMismatch) isRuntimeCredentialOutcome()   {}
func (replacePodCredential) isRuntimeCredentialOutcome()     {}

func (r *Reconciler) ensureRuntimeCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload, needsRuntime bool) (workload, *unready, error) {
	outcome, err := r.decideRuntimeCredential(ctx, sandbox, w, needsRuntime)
	if err != nil {
		return w, nil, err
	}
	return r.applyRuntimeCredential(ctx, sandbox, w, outcome)
}

func (r *Reconciler) decideRuntimeCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload, needsRuntime bool) (runtimeCredentialOutcome, error) {
	switch {
	case w.pod != nil:
		return r.ensurePodRuntimeCredential(ctx, sandbox, w)
	case w.vm != nil:
		return r.ensureVMRuntimeCredential(ctx, sandbox, w, needsRuntime)
	case w.vmi != nil:
		return keepPublishedCredential{gate: &unready{reason: "VMIStopping", message: fmt.Sprintf("VMI %s is still stopping", w.vmi.Name)}}, nil
	case !needsRuntime:
		return revokeRuntimeCredential{}, nil
	}
	template, blocked, err := r.template(ctx, sandbox)
	if err != nil {
		return nil, err
	}
	if blocked != nil {
		return credentialPendingOutcome{gate: *blocked}, nil
	}
	kind := sandboxapi.RunnerCredentialKindPod
	if template.Spec.VM != nil {
		kind = sandboxapi.RunnerCredentialKindVM
	}
	issued, pending, err := r.requestStartCredential(ctx, sandbox)
	if err != nil {
		return nil, err
	}
	if pending != nil {
		return credentialPendingOutcome{gate: *pending}, nil
	}
	return recordStartCredential{issued: issued, kind: kind}, nil
}

func (r *Reconciler) ensurePodRuntimeCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload) (runtimeCredentialOutcome, error) {
	pod := w.pod
	id := pod.Labels[sandboxapi.EnvironmentRuntimeCredentialMetadataKey]
	if !validRuntimeCredentialID(id) {
		// Only the exact previously published legacy incarnation can retain its route.
		return keepPublishedCredential{gate: &unready{reason: "RunnerCredentialMissing", message: fmt.Sprintf("Pod %s has no controller-issued Environment Runtime credential", pod.Name)}}, nil
	}
	if pod.UID == "" {
		return credentialPendingOutcome{gate: *credentialPending("RunnerCredentialPending", fmt.Sprintf("Pod %s has no assigned UID yet", pod.Name))}, nil
	}
	bound := runtimeCredentialBinding(sandbox)
	if bound.PodUID != "" && bound.PodUID != string(pod.UID) {
		return replacePodCredential{message: fmt.Sprintf("Pod %s is not the incarnation this Sandbox accepted", pod.Name)}, nil
	}
	issued, changed, pending, err := r.requestIncarnationCredential(ctx, sandbox, requester.Pod{Name: pod.Name, UID: pod.UID})
	if err != nil {
		return nil, err
	}
	if changed {
		return replacePodCredential{message: fmt.Sprintf("Pod %s has a credential subject its Sandbox no longer accepts", pod.Name)}, nil
	}
	if pending != nil {
		return keepPublishedCredential{gate: pending}, nil
	}
	if issued.ID != id || !issued.Accepted {
		return replacePodCredential{message: fmt.Sprintf("Pod %s was started with a credential its Sandbox no longer issues", pod.Name)}, nil
	}
	return acceptPodCredential{issued: issued}, nil
}

// The VM annotations fence the accepted VMI independently of Sandbox status publication.
func (r *Reconciler) ensureVMRuntimeCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload, needsRuntime bool) (runtimeCredentialOutcome, error) {
	vm := w.vm
	if !vm.DeletionTimestamp.IsZero() {
		return keepPublishedCredential{}, nil
	}
	id := vm.Annotations[sandboxapi.EnvironmentRuntimeCredentialMetadataKey]
	acceptedVMI := vm.Annotations[EnvironmentRuntimeCredentialVMIUIDAnnotation]
	if w.vmi == nil && !vmRunning(vm) {
		if !needsRuntime {
			return revokeRuntimeCredential{}, nil
		}
		issued, pending, err := r.requestStartCredential(ctx, sandbox)
		if err != nil {
			return nil, err
		}
		if pending != nil {
			return credentialPendingOutcome{gate: *pending}, nil
		}
		return recordStartCredential{issued: issued, kind: sandboxapi.RunnerCredentialKindVM}, nil
	}
	if !validRuntimeCredentialID(id) {
		return keepPublishedCredential{gate: &unready{reason: "RunnerCredentialRequired", message: "Environment Runtime mTLS credentials are installed after the legacy VM stops"}}, nil
	}
	vmi := w.vmi
	if vmi == nil {
		return awaitVMI{}, nil
	}
	if vmi.Labels[sandboxapi.EnvironmentRuntimeCredentialMetadataKey] != id {
		return haltCredentialMismatch{message: fmt.Sprintf("VMI %s does not carry the VM's active Environment Runtime credential", vmi.Name)}, nil
	}
	if acceptedVMI != "" && acceptedVMI != string(vmi.UID) {
		return rotateVMICredential{message: fmt.Sprintf("VMI %s was replaced; its credential is being rotated", vmi.Name)}, nil
	}
	binding := requester.VMI{UID: vmi.UID}
	if w.launcher != nil {
		binding.Launcher = &requester.Pod{Name: w.launcher.Name, UID: w.launcher.UID}
	}
	issued, changed, pending, err := r.requestIncarnationCredential(ctx, sandbox, binding)
	if err != nil {
		return nil, err
	}
	if changed {
		return haltCredentialMismatch{message: fmt.Sprintf("VMI %s has a credential subject its Sandbox no longer accepts", vmi.Name)}, nil
	}
	if pending != nil {
		if acceptedVMI == string(vmi.UID) {
			return keepPublishedCredential{gate: pending}, nil
		}
		return credentialPendingOutcome{gate: *pending}, nil
	}
	if issued.ID != id || !issued.Accepted {
		return haltCredentialMismatch{message: fmt.Sprintf("VMI %s was started with a credential its Sandbox no longer issues", vmi.Name)}, nil
	}
	return acceptVMICredential{issued: issued}, nil
}

func credentialPending(reason, message string) *unready {
	return &unready{reason: reason, message: message, retryAfter: runtimeCredentialRetry}
}

func validRuntimeCredentialID(id string) bool {
	return len(id) == 32 && strings.Trim(id, "0123456789abcdef") == ""
}

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
