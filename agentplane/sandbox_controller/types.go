// Package sandboxcontroller is the Sandbox Controller: it reconciles Agentplane's own Sandbox into
// a state PVC that outlives every incarnation and, while the Sandbox is running, one Pod stamped
// from its SandboxTemplate. Contract: SPEC.md.
//
// +kubebuilder:object:generate=true
// +groupName=agentplane.allegedly.works
package sandboxcontroller

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
)

// StateVolume is the volume the controller fills with the Sandbox's state PVC: a Pod's containers
// mount it by name, and a VM guest mounts the virtiofs filesystem of that name.
type StateVolume struct {
	Name   string                           `json:"name"`
	Claim  corev1.PersistentVolumeClaimSpec `json:"claim"`
	Layout VolumeLayout                     `json:"layout"`
}

type VolumeLayoutType string

const (
	DirectoryTree   VolumeLayoutType = "DirectoryTree"
	RawDiskImage    VolumeLayoutType = "RawDiskImage"
	BlockFilesystem VolumeLayoutType = "BlockFilesystem"
)

// VolumeLayout describes contents independently of the workload attachment.
type VolumeLayout struct {
	Type       VolumeLayoutType `json:"type"`
	Filesystem string           `json:"filesystem,omitempty"`
}

// WorkspaceVolume is retained by the Sandbox across workload replacements.
type WorkspaceVolume struct {
	Name   string                           `json:"name"`
	Claim  corev1.PersistentVolumeClaimSpec `json:"claim"`
	Layout VolumeLayout                     `json:"layout"`
}

// SandboxTemplateSpec sets the Sandbox's kind: exactly one of Pod and VM.
type SandboxTemplateSpec struct {
	Pod *corev1.PodTemplateSpec `json:"pod,omitempty"`
	// VM is the VirtualMachine's spec; the controller sets its run strategy from spec.running.
	VM              *kubevirtv1.VirtualMachineSpec `json:"vm,omitempty"`
	StateVolume     StateVolume                    `json:"stateVolume"`
	WorkspaceVolume *WorkspaceVolume               `json:"workspaceVolume,omitempty"`
}

// +kubebuilder:object:root=true
type SandboxTemplate struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              SandboxTemplateSpec `json:"spec"`
}

// +kubebuilder:object:root=true
type SandboxTemplateList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []SandboxTemplate `json:"items"`
}

type SandboxSpec struct {
	// Template names a SandboxTemplate in the Sandbox's namespace, read again at every start.
	Template string `json:"template"`
	Running  bool   `json:"running"`
	// WakeOnSend allows authenticated user and notification sends to resume a stopped Sandbox.
	// Omitted is false so older Sandbox manifests keep wake-on-send disabled.
	WakeOnSend bool `json:"wakeOnSend,omitempty"`
	// WakeGeneration is bumped by whoever wakes the Sandbox. An idle-suspended Sandbox runs again once it
	// differs from the generation it was suspended at.
	WakeGeneration int64 `json:"wakeGeneration,omitempty"`
	// PolicySets names the PolicySets in the Sandbox's namespace its ServiceAccount is bound to.
	PolicySets []string `json:"policySets,omitempty"`
	// SessionDefaults are the Runner Gateway's; the controller keeps them only so its updates
	// round-trip them.
	SessionDefaults *SessionDefaults `json:"sessionDefaults,omitempty"`
	// Bootstrap is the shell source the Sandbox's runner runs once, before its first Session.
	Bootstrap string `json:"bootstrap,omitempty"`
}

type SessionDefaults struct {
	Harness         *string `json:"harness,omitempty"`
	Model           *string `json:"model,omitempty"`
	Cwd             *string `json:"cwd,omitempty"`
	ReasoningEffort *string `json:"reasoningEffort,omitempty"`
	Instructions    *string `json:"instructions,omitempty"`
	SetupScript     *string `json:"setupScript,omitempty"`
}

// Incarnation is the Pod the Sandbox's runner currently runs in: its own Pod, or its VM's
// virt-launcher Pod.
type Incarnation struct {
	Name string `json:"name"`
	UID  string `json:"uid"`
}

// QuiesceAttempt records when the controller last asked this runner incarnation to quiesce.
// It lets the controller persist its retry deadline across reconciles and restarts.
type QuiesceAttempt struct {
	RunnerPodUID string      `json:"runnerPodUid"`
	AttemptedAt  metav1.Time `json:"attemptedAt"`
}

type SandboxStatus struct {
	Volumes     *SandboxVolumes `json:"volumes,omitempty"`
	Incarnation *Incarnation    `json:"incarnation,omitempty"`
	// The UID of the last incarnation whose runner sealed every Session, kept after it stops.
	QuiescedIncarnation *string `json:"quiescedIncarnation,omitempty"`
	// The latest Quiesce request, scoped to its runner incarnation, for the controller's retry deadline.
	QuiesceAttempt *QuiesceAttempt `json:"quiesceAttempt,omitempty"`
	// OperatingMode is whether the controller runs the Sandbox: the one derivation every reader of
	// `spec.running` reads instead.
	OperatingMode OperatingMode `json:"operatingMode,omitempty"`
	// SuspendedBy is set while the controller keeps a Sandbox stopped that `spec.running` says to run, and
	// SuspendedAtWakeGeneration is the `spec.wakeGeneration` it stopped it at.
	SuspendedBy               *SuspendedBy       `json:"suspendedBy,omitempty"`
	SuspendedAtWakeGeneration *int64             `json:"suspendedAtWakeGeneration,omitempty"`
	Conditions                []metav1.Condition `json:"conditions,omitempty"`
}

// WorkspaceBinding projects the creation record and pins the claim's identity.
type WorkspaceBinding struct {
	Name   string       `json:"name"`
	UID    string       `json:"uid"`
	Layout VolumeLayout `json:"layout"`
}

type SandboxVolumes struct {
	State     *StateBinding     `json:"state,omitempty"`
	Workspace *WorkspaceBinding `json:"workspace,omitempty"`
}

// StateBinding projects the state PVC's durable layout record and pins the claim's identity.
type StateBinding struct {
	Name   string       `json:"name"`
	UID    string       `json:"uid"`
	Layout VolumeLayout `json:"layout"`
}

type OperatingMode string

const (
	OperatingModeRunning   OperatingMode = "Running"
	OperatingModeSuspended OperatingMode = "Suspended"
)

type SuspendedBy string

const SuspendedByIdle SuspendedBy = "Idle"

// +kubebuilder:object:root=true
type Sandbox struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              SandboxSpec   `json:"spec"`
	Status            SandboxStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type SandboxList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Sandbox `json:"items"`
}
