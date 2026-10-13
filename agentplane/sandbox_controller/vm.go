package sandboxcontroller

import (
	"context"
	"fmt"
	"slices"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// vm is the Sandbox's VirtualMachine, or nil; a same-name VM it does not control is not its.
func (r *Reconciler) vm(ctx context.Context, sandbox *Sandbox) (*kubevirtv1.VirtualMachine, error) {
	var vm kubevirtv1.VirtualMachine
	if err := r.Get(ctx, client.ObjectKeyFromObject(sandbox), &vm); err != nil {
		return nil, client.IgnoreNotFound(err)
	}
	if !metav1.IsControlledBy(&vm, sandbox) {
		return nil, nil
	}
	return &vm, nil
}

// vmi is the VMI of the Sandbox's VM, or of a VM of its name being deleted, or nil.
func (r *Reconciler) vmi(ctx context.Context, sandbox *Sandbox) (*kubevirtv1.VirtualMachineInstance, error) {
	var vmi kubevirtv1.VirtualMachineInstance
	if err := r.Get(ctx, client.ObjectKeyFromObject(sandbox), &vmi); err != nil {
		return nil, client.IgnoreNotFound(err)
	}
	owner := metav1.GetControllerOf(&vmi)
	if owner == nil || owner.Kind != "VirtualMachine" || owner.Name != sandbox.Name {
		return nil, nil
	}
	return &vmi, nil
}

// launcher is the VMI's live virt-launcher Pod, the runner's incarnation, or nil.
func (r *Reconciler) launcher(ctx context.Context, sandbox *Sandbox, vmi *kubevirtv1.VirtualMachineInstance) (*corev1.Pod, error) {
	if vmi == nil {
		return nil, nil
	}
	var pods corev1.PodList
	if err := r.List(ctx, &pods, client.InNamespace(sandbox.Namespace), client.MatchingLabels{SandboxLabel: sandbox.Name}); err != nil {
		return nil, err
	}
	live := slices.DeleteFunc(pods.Items, func(p corev1.Pod) bool {
		return !metav1.IsControlledBy(&p, vmi) || !p.DeletionTimestamp.IsZero()
	})
	if len(live) != 1 {
		return nil, nil
	}
	return &live[0], nil
}

func vmRunning(vm *kubevirtv1.VirtualMachine) bool {
	return vm != nil && vm.Spec.RunStrategy != nil && *vm.Spec.RunStrategy == kubevirtv1.RunStrategyAlways
}

// haltVM stops the VM and keeps it, with its disks.
func (r *Reconciler) haltVM(ctx context.Context, vm *kubevirtv1.VirtualMachine) error {
	if vm == nil || !vm.DeletionTimestamp.IsZero() || !vmRunning(vm) {
		return nil
	}
	halted := kubevirtv1.RunStrategyHalted
	vm.Spec.Running, vm.Spec.RunStrategy = nil, &halted
	return client.IgnoreNotFound(r.Update(ctx, vm))
}

// startVM creates the Sandbox's VM, or starts its halted one with the template as it is now, so a
// changed image or template applies at this start; a halted VM's disks are kept.
func (r *Reconciler) startVM(ctx context.Context, sandbox *Sandbox, w workload, template *SandboxTemplate) (workload, *unready, error) {
	vm, blocked := renderVM(sandbox, template)
	if blocked != nil {
		return w, blocked, nil
	}
	if w.vm != nil {
		w.vm.Labels, w.vm.Annotations, w.vm.Spec = vm.Labels, vm.Annotations, vm.Spec
		return w, nil, r.Update(ctx, w.vm)
	}
	if err := controllerutil.SetControllerReference(sandbox, vm, r.Scheme()); err != nil {
		return w, nil, err
	}
	if err := r.Create(ctx, vm); err != nil {
		if apierrors.IsAlreadyExists(err) {
			return w, &unready{reason: "VMConflict", message: fmt.Sprintf("VM %s exists and is not this Sandbox's", vm.Name)}, nil
		}
		return w, nil, err
	}
	w.vm = vm
	return w, nil, nil
}

// renderVM is the template's VM for this Sandbox, running, with the state PVC as a virtiofs
// filesystem of the state volume's name. The launcher's ServiceAccount is not set here: KubeVirt
// sets it only from a serviceAccount volume, which would hand the guest the token, so the
// namespace's launcher admission sets the Sandbox's.
func renderVM(sandbox *Sandbox, template *SandboxTemplate) (*kubevirtv1.VirtualMachine, *unready) {
	spec := template.Spec.VM.DeepCopy()
	if spec.Template == nil {
		return nil, &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s has no VM template", template.Name)}
	}
	instance := &spec.Template.Spec
	volume := template.Spec.StateVolume.Name
	if slices.ContainsFunc(instance.Volumes, func(v kubevirtv1.Volume) bool { return v.Name == volume }) ||
		slices.ContainsFunc(instance.Domain.Devices.Filesystems, func(f kubevirtv1.Filesystem) bool { return f.Name == volume }) {
		return nil, &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s defines the state volume %s itself", template.Name, volume)}
	}
	if blocked := withLaunchDisk(sandbox, template, instance); blocked != nil {
		return nil, blocked
	}
	// DataVolume names are per namespace, so each Sandbox's VM gets its own, prefixed with the
	// Sandbox's name. They belong to the VM: kept while it is halted, deleted with it.
	renamed := map[string]string{}
	for i := range spec.DataVolumeTemplates {
		dv := &spec.DataVolumeTemplates[i]
		renamed[dv.Name] = sandbox.Name + "-" + dv.Name
		dv.Name = renamed[dv.Name]
	}
	for _, v := range instance.Volumes {
		if v.DataVolume != nil {
			if name, ok := renamed[v.DataVolume.Name]; ok {
				v.DataVolume.Name = name
			}
		}
	}
	instance.Volumes = append(instance.Volumes, kubevirtv1.Volume{
		Name: volume,
		VolumeSource: kubevirtv1.VolumeSource{PersistentVolumeClaim: &kubevirtv1.PersistentVolumeClaimVolumeSource{
			PersistentVolumeClaimVolumeSource: corev1.PersistentVolumeClaimVolumeSource{ClaimName: StateVolumeName(sandbox.Name)},
		}},
	})
	instance.Domain.Devices.Filesystems = append(instance.Domain.Devices.Filesystems, kubevirtv1.Filesystem{
		Name: volume, Virtiofs: &kubevirtv1.FilesystemVirtiofs{},
	})
	// The VMI and its virt-launcher Pod carry the Sandbox label too.
	spec.Template.ObjectMeta.Labels = withSandboxLabel(spec.Template.ObjectMeta.Labels, sandbox)
	always := kubevirtv1.RunStrategyAlways
	spec.Running, spec.RunStrategy = nil, &always
	meta := owned(sandbox)
	meta.Annotations = map[string]string{RenderedSpecAnnotation: renderedSpec(spec)}
	return &kubevirtv1.VirtualMachine{ObjectMeta: meta, Spec: *spec}, nil
}

func vmCondition(condition *metav1.Condition, sandbox *Sandbox, w workload) {
	vmi := w.vmi
	switch {
	case vmi == nil:
		condition.Reason, condition.Message = "Starting", fmt.Sprintf("VM %s has no VMI yet", w.vm.Name)
	case !vmi.DeletionTimestamp.IsZero() || !sandbox.Spec.Running || !vmRunning(w.vm):
		condition.Reason, condition.Message = "Stopping", fmt.Sprintf("VMI %s is stopping", vmi.Name)
	case vmi.Status.Phase == kubevirtv1.Running && w.launcher != nil:
		condition.Status, condition.Reason = metav1.ConditionTrue, "VMRunning"
		condition.Message = fmt.Sprintf("VMI %s is running in Pod %s", vmi.Name, w.launcher.Name)
	case vmi.Status.Phase == kubevirtv1.Succeeded || vmi.Status.Phase == kubevirtv1.Failed:
		condition.Reason, condition.Message = "VMEnded", fmt.Sprintf("VMI %s ended (%s); KubeVirt starts a new one", vmi.Name, vmi.Status.Phase)
	default:
		condition.Reason, condition.Message = "Starting", fmt.Sprintf("VMI %s is not running yet (%s)", vmi.Name, vmi.Status.Phase)
	}
}
