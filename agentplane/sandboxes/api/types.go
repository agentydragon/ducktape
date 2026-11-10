// Package api defines Agentplane's shared Sandbox and SandboxTemplate Kubernetes API.
package api

import (
	"fmt"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
)

// StateVolume is the controller-owned PVC for retained environment state. Pod and VM backends keep
// a directory tree; Firecracker stores the guest state disk and its checkpoints.
type StateVolume struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// +kubebuilder:validation:Schemaless
	// +kubebuilder:pruning:PreserveUnknownFields
	// +kubebuilder:validation:Type=object
	Claim corev1.PersistentVolumeClaimSpec `json:"claim"`
	// +kubebuilder:validation:XValidation:rule="self.type == 'DirectoryTree' || self.type == 'RawDiskImage'",message="state volume requires DirectoryTree or RawDiskImage"
	Layout VolumeLayout `json:"layout"`
}

type VolumeLayoutType string

const (
	DirectoryTree   VolumeLayoutType = "DirectoryTree"
	RawDiskImage    VolumeLayoutType = "RawDiskImage"
	BlockFilesystem VolumeLayoutType = "BlockFilesystem"
)

// VolumeLayout describes contents independently of the workload attachment.
// +kubebuilder:validation:XValidation:rule="self.type == 'DirectoryTree' ? !has(self.filesystem) : has(self.filesystem)",message="DirectoryTree omits filesystem; disk layouts require it"
type VolumeLayout struct {
	// +kubebuilder:validation:Enum=DirectoryTree;RawDiskImage;BlockFilesystem
	Type VolumeLayoutType `json:"type"`
	// +kubebuilder:validation:Enum=ext4
	Filesystem string `json:"filesystem,omitempty"`
}

// WorkspaceVolume is retained by the Sandbox across workload replacements.
// +kubebuilder:validation:XValidation:rule="self.layout.type == 'BlockFilesystem' ? has(self.claim.volumeMode) && self.claim.volumeMode == 'Block' : !has(self.claim.volumeMode) || self.claim.volumeMode == 'Filesystem'",message="BlockFilesystem needs Block volumeMode; other layouts use Filesystem"
type WorkspaceVolume struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// +kubebuilder:pruning:PreserveUnknownFields
	Claim  corev1.PersistentVolumeClaimSpec `json:"claim"`
	Layout VolumeLayout                     `json:"layout"`
}

// FirecrackerTemplate describes a launcher Pod and the two immutable images it mounts.
type FirecrackerTemplate struct {
	// +kubebuilder:validation:Schemaless
	// +kubebuilder:pruning:PreserveUnknownFields
	// +kubebuilder:validation:Type=object
	Pod corev1.PodTemplateSpec `json:"pod"`
	// +kubebuilder:validation:MinLength=1
	BootImage string `json:"bootImage"`
	// +kubebuilder:validation:MinLength=1
	RunnerImage string `json:"runnerImage"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=32
	VCPUCount int32 `json:"vcpuCount"`
	// +kubebuilder:validation:Minimum=128
	MemoryMiB int64 `json:"memoryMiB"`
}

// SandboxTemplateSpec sets the Sandbox's kind: exactly one of Pod, VM and Firecracker.
// +kubebuilder:validation:ExactlyOneOf=pod;vm;firecracker
// +kubebuilder:validation:XValidation:rule="!has(self.workspaceVolume) || !has(self.pod) || self.workspaceVolume.layout.type == 'DirectoryTree'",message="Pod workspaces require DirectoryTree layout"
// +kubebuilder:validation:XValidation:rule="!has(self.firecracker) || (self.stateVolume.layout.type == 'RawDiskImage' && self.stateVolume.layout.filesystem == 'ext4')",message="Firecracker state requires an ext4 RawDiskImage"
// +kubebuilder:validation:XValidation:rule="has(self.firecracker) || (self.stateVolume.layout.type == 'DirectoryTree' && !has(self.stateVolume.layout.filesystem))",message="Pod and VM state require DirectoryTree without a filesystem"
// +kubebuilder:validation:XValidation:rule="!has(self.firecracker) || !has(self.workspaceVolume) || (self.workspaceVolume.layout.type != 'DirectoryTree' && self.workspaceVolume.layout.filesystem == 'ext4')",message="Firecracker workspace requires an ext4 disk layout"
// +kubebuilder:validation:XValidation:rule="!has(self.firecracker) || has(self.workspaceVolume)",message="Firecracker requires a workspace volume"
type SandboxTemplateSpec struct {
	// +kubebuilder:validation:Schemaless
	// +kubebuilder:pruning:PreserveUnknownFields
	// +kubebuilder:validation:Type=object
	Pod *corev1.PodTemplateSpec `json:"pod,omitempty"`
	// VM is the VirtualMachine's spec; the controller sets its run strategy from spec.running.
	// +kubebuilder:validation:Schemaless
	// +kubebuilder:pruning:PreserveUnknownFields
	// +kubebuilder:validation:Type=object
	VM              *kubevirtv1.VirtualMachineSpec `json:"vm,omitempty"`
	Firecracker     *FirecrackerTemplate           `json:"firecracker,omitempty"`
	StateVolume     StateVolume                    `json:"stateVolume"`
	WorkspaceVolume *WorkspaceVolume               `json:"workspaceVolume,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced,path=sandboxtemplates,singular=sandboxtemplate
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
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
	// +kubebuilder:validation:MinLength=1
	Template string `json:"template"`
	// +kubebuilder:default=false
	// +kubebuilder:validation:Optional
	Running bool `json:"running"`
	// WakeOnSend allows queued delivery to request an automatic resume when it needs a suspended
	// Sandbox. Caller lifecycle authority is checked separately, and a requested stop stays sticky.
	// Omitted is false so older Sandbox manifests keep automatic wake disabled.
	// +kubebuilder:default=false
	WakeOnSend bool `json:"wakeOnSend,omitempty"`
	// ResumePolicy controls whether the exact live execution ServiceAccount or Identity may request resume.
	// Omitted policy and fields default to deny.
	ResumePolicy *ResumePolicy `json:"resumePolicy,omitempty"`
	// WakeGeneration is bumped by whoever wakes the Sandbox. An idle-suspended Sandbox runs again once it
	// differs from the generation it was suspended at.
	// +kubebuilder:default=0
	// +kubebuilder:validation:Minimum=0
	WakeGeneration int64 `json:"wakeGeneration,omitempty"`
	// IdleSuspendAfter is how long the Sandbox's runner must stay idle before the controller suspends the
	// Sandbox, at least a minute. Unset, the controller never suspends it for idleness.
	// +kubebuilder:validation:Type=string
	// +kubebuilder:validation:MaxLength=32
	// +kubebuilder:validation:XValidation:rule="duration(self) >= duration('1m')",message="idleSuspendAfter is at least one minute"
	IdleSuspendAfter *metav1.Duration `json:"idleSuspendAfter,omitempty"`
	// Identity is who the Sandbox's workloads act as. It may be set, changed or cleared only while
	// nothing runs, so an incarnation never changes identity; the next start acts as the new one.
	// "Nothing runs" is the controller's last status report: never reconciled, or stopStage Stopped.
	Identity *IdentityRef `json:"identity,omitempty"`
	// PolicySets names the PolicySets in the Sandbox's namespace its ServiceAccount is bound to.
	// +listType=set
	// +kubebuilder:validation:items:MinLength=1
	PolicySets []string `json:"policySets,omitempty"`
	// InboxTags are the groups the Sandbox's Session inboxes take messages as: a `session_inbox_access` rule
	// targeting one of them covers the Sandbox while it holds the tag.
	// +listType=set
	// +kubebuilder:validation:items:MinLength=1
	// +kubebuilder:validation:items:MaxLength=63
	InboxTags []string `json:"inboxTags,omitempty"`
	// Bootstrap is the shell source the Sandbox's runner runs once, before its first Session.
	// +kubebuilder:validation:MaxLength=65536
	Bootstrap string `json:"bootstrap,omitempty"`
}

// IdentityRef names an Identity in the Sandbox's namespace.
type IdentityRef struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
}

// ResumePolicy contains independent explicit-resume permissions for the Sandbox's own execution
// ServiceAccount and its own Identity.
type ResumePolicy struct {
	// AllowOwnServiceAccount permits the Sandbox's exact live execution ServiceAccount to request
	// resume while the Sandbox is idle-suspended. It does not authorize overriding a requested stop.
	// +kubebuilder:default=false
	// +kubebuilder:validation:Optional
	AllowOwnServiceAccount bool `json:"allowOwnServiceAccount,omitempty"`
	// AllowOwnIdentity permits the exact live Identity incarnation `spec.identity` names to request
	// resume while the Sandbox is idle-suspended. It does not authorize overriding a requested stop.
	// +kubebuilder:default=false
	// +kubebuilder:validation:Optional
	AllowOwnIdentity bool `json:"allowOwnIdentity,omitempty"`
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

// RunnerCredentialKind is the workload kind a runner credential generation was issued for.
// +kubebuilder:validation:Enum=Pod;VM
type RunnerCredentialKind string

const (
	RunnerCredentialKindPod RunnerCredentialKind = "Pod"
	RunnerCredentialKindVM  RunnerCredentialKind = "VM"
)

// RunnerCredential is the incarnation the controller accepted for the Sandbox's WorkloadCredential,
// which holds the credential ID, URI and Secret.
type RunnerCredential struct {
	Kind RunnerCredentialKind `json:"kind"`
	// PodUID is the Pod the controller started and accepted; a Pod of the Sandbox's name with another
	// UID is a stale replacement.
	PodUID string `json:"podUid,omitempty"`
	// LastVMIUID is the last VMI accepted for the Sandbox's VM.
	LastVMIUID string `json:"lastVmiUid,omitempty"`
}

// AutosuspendBlockerReason is a current runner fact that keeps the Sandbox from being idle.
// +kubebuilder:validation:Enum=ActiveTurn;PendingCommand;Setup;Activation;Bootstrap;Admission;ExecutorRelay
type AutosuspendBlockerReason string

const (
	AutosuspendBlockerActiveTurn     AutosuspendBlockerReason = "ActiveTurn"
	AutosuspendBlockerPendingCommand AutosuspendBlockerReason = "PendingCommand"
	AutosuspendBlockerSetup          AutosuspendBlockerReason = "Setup"
	AutosuspendBlockerActivation     AutosuspendBlockerReason = "Activation"
	AutosuspendBlockerBootstrap      AutosuspendBlockerReason = "Bootstrap"
	AutosuspendBlockerAdmission      AutosuspendBlockerReason = "Admission"
	AutosuspendBlockerExecutorRelay  AutosuspendBlockerReason = "ExecutorRelay"
)

// AutosuspendBlocker is one live reason the controller must wait before suspending a Sandbox.
type AutosuspendBlocker struct {
	Reason    AutosuspendBlockerReason `json:"reason"`
	SessionID *string                  `json:"sessionId,omitempty"`
}

// ValidateAutosuspendBlockers rejects malformed and duplicate runner Lease facts.
func ValidateAutosuspendBlockers(blockers []AutosuspendBlocker) error {
	seen := make(map[string]struct{}, len(blockers))
	for _, blocker := range blockers {
		switch blocker.Reason {
		case AutosuspendBlockerActiveTurn, AutosuspendBlockerPendingCommand, AutosuspendBlockerSetup, AutosuspendBlockerActivation:
			if blocker.SessionID == nil || *blocker.SessionID == "" {
				return fmt.Errorf("autosuspend blocker %q requires a session ID", blocker.Reason)
			}
		case AutosuspendBlockerBootstrap, AutosuspendBlockerAdmission, AutosuspendBlockerExecutorRelay:
			if blocker.SessionID != nil {
				return fmt.Errorf("autosuspend blocker %q cannot have a session ID", blocker.Reason)
			}
		default:
			return fmt.Errorf("unknown autosuspend blocker reason %q", blocker.Reason)
		}
		sessionID := ""
		if blocker.SessionID != nil {
			sessionID = *blocker.SessionID
		}
		key := string(blocker.Reason) + "\x00" + sessionID
		if _, duplicate := seen[key]; duplicate {
			return fmt.Errorf("duplicate autosuspend blocker %q", blocker.Reason)
		}
		seen[key] = struct{}{}
	}
	return nil
}

// +kubebuilder:validation:XValidation:rule="has(self.suspendedAtWakeGeneration) == (has(self.suspendedBy) && self.suspendedBy == 'Idle')",message="suspendedAtWakeGeneration is set exactly when suspendedBy is Idle"
type SandboxStatus struct {
	Volumes     *SandboxVolumes `json:"volumes,omitempty"`
	Incarnation *Incarnation    `json:"incarnation,omitempty"`
	// FirecrackerSuspension retains the admission barrier across controller retries,
	// checkpoint and thaw. Released keeps the generation for the next attempt.
	FirecrackerSuspension *FirecrackerSuspension `json:"firecrackerSuspension,omitempty"`
	// IdleSuspendAt is the controller's next attempt to suspend an idle Sandbox, when one is scheduled.
	IdleSuspendAt *metav1.Time `json:"idleSuspendAt,omitempty"`
	// IdleSuspendBlockers are the live runner facts keeping autosuspend from suspending this Sandbox.
	// +listType=atomic
	IdleSuspendBlockers []AutosuspendBlocker `json:"idleSuspendBlockers,omitempty"`
	// The UID of the last incarnation whose runner sealed every Session, kept after it stops.
	QuiescedIncarnation *string `json:"quiescedIncarnation,omitempty"`
	// The latest Quiesce request, scoped to its runner incarnation, for the controller's retry deadline.
	QuiesceAttempt *QuiesceAttempt `json:"quiesceAttempt,omitempty"`
	// RunnerCredential is the incarnation the controller accepted. It lives under status so writers of
	// the Sandbox's spec and metadata cannot rebind it.
	RunnerCredential *RunnerCredential `json:"runnerCredential,omitempty"`
	// OperatingMode is whether the controller runs the Sandbox: the one derivation every reader of
	// `spec.running` reads instead.
	OperatingMode OperatingMode `json:"operatingMode,omitempty"`
	// SuspendedBy is why the Sandbox is suspended. `Idle` keeps a Sandbox stopped that `spec.running` says to run,
	// and SuspendedAtWakeGeneration is the `spec.wakeGeneration` it stopped it at.
	SuspendedBy *SuspendedBy `json:"suspendedBy,omitempty"`
	// +kubebuilder:validation:Minimum=0
	SuspendedAtWakeGeneration *int64 `json:"suspendedAtWakeGeneration,omitempty"`
	// StopStage is how far the controller has taken stopping the Sandbox's workload; absent while
	// the workload runs or starts. Readers dispatch on it; condition reasons are display only.
	StopStage StopStage `json:"stopStage,omitempty"`
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

type FirecrackerSuspension struct {
	// +kubebuilder:validation:Minimum=1
	Generation int64                      `json:"generation"`
	IdleSince  metav1.MicroTime           `json:"idleSince"`
	Phase      FirecrackerSuspensionPhase `json:"phase"`
}

// +kubebuilder:validation:Enum=Preparing;Checkpointing;Resuming;Released
type FirecrackerSuspensionPhase string

const (
	FirecrackerSuspendPreparing     FirecrackerSuspensionPhase = "Preparing"
	FirecrackerSuspendCheckpointing FirecrackerSuspensionPhase = "Checkpointing"
	FirecrackerSuspendResuming      FirecrackerSuspensionPhase = "Resuming"
	FirecrackerSuspendReleased      FirecrackerSuspensionPhase = "Released"
)

// WorkspaceBinding projects the creation record and pins the claim's identity.
type WorkspaceBinding struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// +kubebuilder:validation:MinLength=1
	UID    string       `json:"uid"`
	Layout VolumeLayout `json:"layout"`
}

type SandboxVolumes struct {
	State     *StateBinding     `json:"state,omitempty"`
	Workspace *WorkspaceBinding `json:"workspace,omitempty"`
}

// StateBinding projects the state PVC's durable layout record and pins the claim's identity.
type StateBinding struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// +kubebuilder:validation:MinLength=1
	UID string `json:"uid"`
	// +kubebuilder:validation:XValidation:rule="self.type == 'DirectoryTree' || self.type == 'RawDiskImage'",message="state volume requires DirectoryTree or RawDiskImage"
	Layout VolumeLayout `json:"layout"`
}

// +kubebuilder:validation:Enum=Running;Suspended
type OperatingMode string

const (
	OperatingModeRunning   OperatingMode = "Running"
	OperatingModeSuspended OperatingMode = "Suspended"
)

// +kubebuilder:validation:Enum=Idle;Requested
type SuspendedBy string

const (
	// The controller suspended the Sandbox because its runner stayed idle for `spec.idleSuspendAfter`.
	SuspendedByIdle SuspendedBy = "Idle"
	// A requester cleared `spec.running`.
	SuspendedByRequested SuspendedBy = "Requested"
)

// +kubebuilder:validation:Enum=Quiescing;Deleting;Stopped
type StopStage string

const (
	// The controller is sealing the current incarnation's Sessions before it stops or deletes it. A
	// Sandbox resumed meanwhile still waits for the seal and gets a new incarnation.
	StopStageQuiescing StopStage = "Quiescing"
	// The Sandbox is being deleted and has no Session left to seal; the controller deletes its
	// workload and runner credentials.
	StopStageDeleting StopStage = "Deleting"
	// No Pod or VM runs; the state volume is kept.
	StopStageStopped StopStage = "Stopped"
)

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced,path=sandboxes,singular=sandbox
// +kubebuilder:printcolumn:name="Template",type=string,JSONPath=".spec.template"
// +kubebuilder:printcolumn:name="Desired",type=boolean,JSONPath=".spec.running"
// +kubebuilder:printcolumn:name="Running",type=string,JSONPath=".status.conditions[?(@.type==\"Running\")].reason"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
// +kubebuilder:validation:XValidation:rule="size(self.metadata.name) <= 63",message="a Sandbox name is at most 63 characters"
// +kubebuilder:validation:XValidation:rule="(has(self.spec.identity) == has(oldSelf.spec.identity) && (!has(self.spec.identity) || self.spec.identity == oldSelf.spec.identity)) || !has(oldSelf.status) || !has(oldSelf.status.operatingMode) || (has(oldSelf.status.stopStage) && oldSelf.status.stopStage == 'Stopped')",message="spec.identity changes only while nothing runs"
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
