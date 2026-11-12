package sandboxcontroller

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/requester"
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
	RunnerTrustBundleName             = "cluster-internal-ca-bundle"
	RunnerTrustBundleKey              = "ca-certificates.crt"

	// The workload CA alone, which verifies the Gateway's client SVID at the runner's control
	// listener and, in the egress sidecar or launcher relay, the server SVIDs a runner dials; the
	// cluster bundle above verifies only the Runtime's own chain. A Pod gets it as
	// RunnerWorkloadTrustFile beside ca.crt, a VM as its own filesystem.
	EnvironmentRuntimeWorkloadTrustVolumeName = "runner-workload-trust"
	RunnerWorkloadTrustBundleName             = "agentplane-workload-ca-bundle"
	RunnerWorkloadTrustFile                   = "workload-ca.crt"

	// A VM's active credential and the VMI accepted for it. The Sandbox's binding is in
	// status.runnerCredential.
	RunnerCredentialVMIUIDAnnotation     = "agentplane.allegedly.works/environment-runtime-credential-vmi-uid"
	RunnerCredentialLastVMIUIDAnnotation = "agentplane.allegedly.works/environment-runtime-credential-last-vmi-uid"

	runnerCredentialRetry = 5 * time.Second
)

// runnerCredential is the incarnation the Sandbox accepted, or none.
func runnerCredential(sandbox *sandboxapi.Sandbox) sandboxapi.RunnerCredential {
	if binding := sandbox.Status.RunnerCredential; binding != nil {
		return *binding
	}
	return sandboxapi.RunnerCredential{}
}

// setRunnerCredential writes the binding through the status subresource, which only the controller
// may write.
func (r *Reconciler) setRunnerCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, binding sandboxapi.RunnerCredential) error {
	sandbox.Status.RunnerCredential = &binding
	return r.Status().Update(ctx, sandbox)
}

// credentialOwner is the Sandbox as its WorkloadCredential's owner: the record takes the Sandbox's
// name and goes with it.
func credentialOwner(sandbox *sandboxapi.Sandbox) requester.Owner {
	return requester.OwnedBy(sandbox, sandboxapi.GroupVersion.WithKind(sandboxapi.SandboxKind))
}

// requestStartCredential asks for the unbound credential the Sandbox's next incarnation starts
// with. A record naming another identity is revoked first, since a record's subject changes only
// with a new binding.
func (r *Reconciler) requestStartCredential(ctx context.Context, sandbox *sandboxapi.Sandbox) (requester.Issued, *unready, error) {
	subject, changed, blocked, err := r.credentialSubject(ctx, sandbox, false)
	if err != nil || blocked != nil {
		return requester.Issued{}, blocked, err
	}
	if changed {
		return requester.Issued{}, credentialPending("IdentityChanged", "the WorkloadCredential names an identity the Sandbox no longer names and is being revoked"),
			r.Credentials.Revoke(ctx, credentialOwner(sandbox))
	}
	return r.requestCredential(ctx, sandbox, subject, nil)
}

// requestIncarnationCredential asks for the credential bound to the incarnation the Sandbox
// accepts. changed says the incarnation acts as another identity than the Sandbox now names, and
// must go instead.
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
func (r *Reconciler) requestCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, subject requester.Subject, binding requester.Binding) (requester.Issued, *unready, error) {
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

// ensureRunnerCredential decides which incarnation the Sandbox accepts and binds its
// WorkloadCredential to it; the Policy Controller issues the credential. Before an incarnation
// starts, it records the unbound credential to start it with in w.credential. Once the binding
// leaves an accepted incarnation the record's credential ID changes, so a replacement never runs
// with, nor revives, an earlier incarnation's key.
func (r *Reconciler) ensureRunnerCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload, needsRunner bool) (workload, *unready, error) {
	switch {
	case w.pod != nil:
		return r.ensurePodRunnerCredential(ctx, sandbox, w)
	case w.vm != nil:
		return r.ensureVMRunnerCredential(ctx, sandbox, w, needsRunner)
	case w.vmi != nil:
		w.runnerIncarnationPublished = previouslyPublishedRunnerIncarnation(sandbox, w)
		return w, &unready{reason: "VMIStopping", message: fmt.Sprintf("VMI %s is still stopping", w.vmi.Name)}, nil
	case !needsRunner:
		// Nothing runs, nor is to: no earlier incarnation's certificate keeps verifying.
		return w, nil, r.Credentials.Revoke(ctx, credentialOwner(sandbox))
	}
	template, blocked, err := r.template(ctx, sandbox)
	if err != nil || blocked != nil {
		return w, blocked, err
	}
	kind := sandboxapi.RunnerCredentialKindPod
	if template.Spec.VM != nil {
		kind = sandboxapi.RunnerCredentialKindVM
	}
	issued, pending, err := r.requestStartCredential(ctx, sandbox)
	if err != nil || pending != nil {
		return w, pending, err
	}
	if bound := runnerCredential(sandbox); bound.Kind != kind || bound.PodUID != "" {
		bound.Kind, bound.PodUID = kind, ""
		if err := r.setRunnerCredential(ctx, sandbox, bound); err != nil {
			return w, nil, err
		}
	}
	w.credential = issued
	return w, nil, nil
}

func (r *Reconciler) ensurePodRunnerCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload) (workload, *unready, error) {
	pod := w.pod
	id := pod.Labels[sandboxapi.EnvironmentRuntimeCredentialMetadataKey]
	if !validRunnerCredentialID(id) {
		// A legacy runner can stay published only while it is the exact incarnation already
		// recorded in status. A replacement without a credential must never inherit that route.
		w.runnerIncarnationPublished = previouslyPublishedRunnerIncarnation(sandbox, w)
		return w, &unready{reason: "RunnerCredentialMissing", message: fmt.Sprintf("Pod %s has no controller-issued runner credential", pod.Name)}, nil
	}
	if pod.UID == "" {
		return w, credentialPending("RunnerCredentialPending", fmt.Sprintf("Pod %s has no assigned UID yet", pod.Name)), nil
	}
	bound := runnerCredential(sandbox)
	if bound.PodUID != "" && bound.PodUID != string(pod.UID) {
		return r.replaceStalePod(ctx, w, fmt.Sprintf("Pod %s is not the incarnation this Sandbox accepted", pod.Name))
	}
	issued, changed, pending, err := r.requestIncarnationCredential(ctx, sandbox, requester.Pod{Name: pod.Name, UID: pod.UID})
	if err != nil {
		return w, nil, err
	}
	if changed {
		return r.replaceStalePod(ctx, w, fmt.Sprintf("Pod %s acts as an identity its Sandbox no longer names", pod.Name))
	}
	if pending != nil {
		// A pending reissue, such as a renewal, keeps the route of the incarnation already published.
		w.runnerIncarnationPublished = previouslyPublishedRunnerIncarnation(sandbox, w)
		return w, pending, nil
	}
	if issued.ID != id || !issued.Accepted {
		return r.replaceStalePod(ctx, w, fmt.Sprintf("Pod %s was started with a credential its Sandbox no longer issues", pod.Name))
	}
	if bound.Kind != sandboxapi.RunnerCredentialKindPod || bound.PodUID == "" {
		bound.Kind, bound.PodUID = sandboxapi.RunnerCredentialKindPod, string(pod.UID)
		if err := r.setRunnerCredential(ctx, sandbox, bound); err != nil {
			return w, nil, err
		}
	}
	w.credential, w.runnerCredentialAccepted, w.runnerIncarnationPublished = issued, true, true
	return w, nil, nil
}

// replaceStalePod deletes a Pod whose credential its Sandbox does not accept. Once it is gone, the
// no-Pod path asks for a fresh credential before creating its replacement. A Firecracker launcher
// is kept, with its checkpoint, for an operator to recover.
func (r *Reconciler) replaceStalePod(ctx context.Context, w workload, message string) (workload, *unready, error) {
	if isFirecrackerPod(w.pod) {
		return w, &unready{reason: "RunnerCredentialMismatch", message: message + "; preserving the Firecracker launcher and its checkpoint"}, nil
	}
	if err := r.deleteIfLive(ctx, w.pod); err != nil {
		return w, nil, err
	}
	return w, &unready{reason: "RunnerCredentialMismatch", message: message + " and is being replaced"}, nil
}

// ensureVMRunnerCredential keeps the VM's template on the record's credential while it is stopped,
// and accepts the first VMI started with that credential. The VM's annotations record which
// credential its template carries and which VMI was accepted for it.
func (r *Reconciler) ensureVMRunnerCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload, needsRunner bool) (workload, *unready, error) {
	vm := w.vm
	if !vm.DeletionTimestamp.IsZero() {
		// KubeVirt owns a deleting VM. Keep only the exact published launcher in status; never
		// bind or start a replacement while finalization is underway.
		w.runnerIncarnationPublished = previouslyPublishedRunnerIncarnation(sandbox, w)
		return w, nil, nil
	}
	id := vm.Annotations[sandboxapi.EnvironmentRuntimeCredentialMetadataKey]
	acceptedVMI := vm.Annotations[RunnerCredentialVMIUIDAnnotation]
	if w.vmi == nil && !vmRunning(vm) {
		if !needsRunner {
			return w, nil, r.Credentials.Revoke(ctx, credentialOwner(sandbox))
		}
		// The next run starts with the record's unbound credential, which is fresh once an earlier
		// VMI was accepted; startVM renders it into the template.
		issued, pending, err := r.requestStartCredential(ctx, sandbox)
		if err != nil || pending != nil {
			return w, pending, err
		}
		if issued.ID != id || acceptedVMI != "" {
			ensureAnnotations(&vm.ObjectMeta)
			if acceptedVMI != "" {
				vm.Annotations[RunnerCredentialLastVMIUIDAnnotation] = acceptedVMI
			}
			vm.Annotations[sandboxapi.EnvironmentRuntimeCredentialMetadataKey] = issued.ID
			vm.Annotations[RunnerCredentialVMIUIDAnnotation] = ""
			if err := r.Update(ctx, vm); err != nil {
				return w, nil, err
			}
		}
		if bound := runnerCredential(sandbox); bound.Kind != sandboxapi.RunnerCredentialKindVM || bound.PodUID != "" {
			bound.Kind, bound.PodUID = sandboxapi.RunnerCredentialKindVM, ""
			if err := r.setRunnerCredential(ctx, sandbox, bound); err != nil {
				return w, nil, err
			}
		}
		w.credential = issued
		return w, nil, nil
	}
	if !validRunnerCredentialID(id) {
		// Let an incarnation from before runner credentials finish through the normal quiesce and
		// stop path; the stopped path then installs one.
		w.runnerIncarnationPublished = previouslyPublishedRunnerIncarnation(sandbox, w)
		return w, &unready{reason: "RunnerCredentialRequired", message: "runner mTLS credentials are installed after the legacy VM stops"}, nil
	}
	vmi := w.vmi
	if vmi == nil {
		// KubeVirt has yet to create the running VM's VMI.
		return w, nil, nil
	}
	if vmi.Labels[sandboxapi.EnvironmentRuntimeCredentialMetadataKey] != id {
		// A VMI of an earlier template may still be stopping. Keep the VM halted and wait; never
		// accept it or bind the record to it.
		if err := r.haltVM(ctx, vm); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "RunnerCredentialMismatch", message: fmt.Sprintf("VMI %s does not carry the VM's active runner credential", vmi.Name)}, nil
	}
	if acceptedVMI != "" && acceptedVMI != string(vmi.UID) {
		// KubeVirt recreated a VMI from the accepted one's template. Stop it and unbind the record,
		// which revokes the credential the template carries; the next start gets a fresh one.
		ensureAnnotations(&vm.ObjectMeta)
		vm.Annotations[RunnerCredentialLastVMIUIDAnnotation] = acceptedVMI
		vm.Annotations[RunnerCredentialVMIUIDAnnotation] = ""
		if vmRunning(vm) {
			halted := kubevirtv1.RunStrategyHalted
			vm.Spec.Running, vm.Spec.RunStrategy = nil, &halted
		}
		if err := r.Update(ctx, vm); err != nil {
			return w, nil, err
		}
		if _, _, err := r.requestStartCredential(ctx, sandbox); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "RunnerCredentialMismatch", message: fmt.Sprintf("VMI %s was replaced; its credential is being rotated", vmi.Name)}, nil
	}
	binding := requester.VMI{UID: vmi.UID}
	if w.launcher != nil {
		binding.Launcher = &requester.Pod{Name: w.launcher.Name, UID: w.launcher.UID}
	}
	issued, changed, pending, err := r.requestIncarnationCredential(ctx, sandbox, binding)
	if err != nil {
		return w, nil, err
	}
	if changed {
		if err := r.haltVM(ctx, vm); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "RunnerCredentialMismatch", message: fmt.Sprintf("VMI %s acts as an identity its Sandbox no longer names", vmi.Name)}, nil
	}
	if pending != nil {
		// A pending reissue, or naming the launcher, keeps the route of the accepted incarnation.
		if acceptedVMI == string(vmi.UID) {
			w.runnerIncarnationPublished = previouslyPublishedRunnerIncarnation(sandbox, w)
		}
		return w, pending, nil
	}
	if issued.ID != id || !issued.Accepted {
		if err := r.haltVM(ctx, vm); err != nil {
			return w, nil, err
		}
		return w, &unready{reason: "RunnerCredentialMismatch", message: fmt.Sprintf("VMI %s was started with a credential its Sandbox no longer issues", vmi.Name)}, nil
	}
	if acceptedVMI == "" {
		// The first VMI for a credential is accepted only when KubeVirt copied the credential label
		// from this VM's template.
		ensureAnnotations(&vm.ObjectMeta)
		vm.Annotations[RunnerCredentialVMIUIDAnnotation] = string(vmi.UID)
		vm.Annotations[RunnerCredentialLastVMIUIDAnnotation] = string(vmi.UID)
		if err := r.Update(ctx, vm); err != nil {
			return w, nil, err
		}
	}
	if bound := runnerCredential(sandbox); bound.Kind != sandboxapi.RunnerCredentialKindVM || bound.LastVMIUID != string(vmi.UID) || bound.PodUID != "" {
		bound.Kind, bound.LastVMIUID, bound.PodUID = sandboxapi.RunnerCredentialKindVM, string(vmi.UID), ""
		if err := r.setRunnerCredential(ctx, sandbox, bound); err != nil {
			return w, nil, err
		}
	}
	w.credential, w.runnerCredentialAccepted, w.runnerIncarnationPublished = issued, true, true
	return w, nil, nil
}

func credentialPending(reason, message string) *unready {
	return &unready{reason: reason, message: message, retryAfter: runnerCredentialRetry}
}

func validRunnerCredentialID(id string) bool {
	return len(id) == 32 && strings.Trim(id, "0123456789abcdef") == ""
}

func withRunnerTLS(template *sandboxapi.SandboxTemplate, spec *corev1.PodSpec, secretName string) *unready {
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
		return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s defines the reserved runner TLS volume or mount", template.Name)}
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
					LocalObjectReference: corev1.LocalObjectReference{Name: RunnerTrustBundleName},
					Items:                []corev1.KeyToPath{{Key: RunnerTrustBundleKey, Path: "ca.crt"}},
				}},
				{ConfigMap: &corev1.ConfigMapProjection{
					LocalObjectReference: corev1.LocalObjectReference{Name: RunnerWorkloadTrustBundleName},
					Items:                []corev1.KeyToPath{{Key: RunnerTrustBundleKey, Path: RunnerWorkloadTrustFile}},
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

func withRunnerTLSDisk(template *sandboxapi.SandboxTemplate, instance *kubevirtv1.VirtualMachineInstanceSpec, secretName string) *unready {
	reserved := []string{EnvironmentRuntimeTLSVolumeName, EnvironmentRuntimeTrustVolumeName, EnvironmentRuntimeWorkloadTrustVolumeName}
	if slices.ContainsFunc(instance.Volumes, func(volume kubevirtv1.Volume) bool {
		return slices.Contains(reserved, volume.Name)
	}) || slices.ContainsFunc(instance.Domain.Devices.Filesystems, func(filesystem kubevirtv1.Filesystem) bool {
		return slices.Contains(reserved, filesystem.Name)
	}) {
		return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s defines the reserved runner TLS volume", template.Name)}
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
			LocalObjectReference: corev1.LocalObjectReference{Name: RunnerTrustBundleName},
		}},
	})
	instance.Domain.Devices.Filesystems = append(instance.Domain.Devices.Filesystems, kubevirtv1.Filesystem{
		Name: EnvironmentRuntimeTLSVolumeName, Virtiofs: &kubevirtv1.FilesystemVirtiofs{},
	})
	instance.Volumes = append(instance.Volumes, kubevirtv1.Volume{
		Name: EnvironmentRuntimeWorkloadTrustVolumeName,
		VolumeSource: kubevirtv1.VolumeSource{ConfigMap: &kubevirtv1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: RunnerWorkloadTrustBundleName},
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
