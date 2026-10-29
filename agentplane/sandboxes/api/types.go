// Package api defines Agentplane's shared Sandbox and SandboxTemplate Kubernetes API.
package api

import (
	"fmt"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
)

// StateVolume is the volume the controller fills with the Sandbox's state PVC: a Pod's containers
// mount it by name, and a VM guest mounts the virtiofs filesystem of that name.
type StateVolume struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// +kubebuilder:validation:Schemaless
	// +kubebuilder:pruning:PreserveUnknownFields
	// +kubebuilder:validation:Type=object
	Claim corev1.PersistentVolumeClaimSpec `json:"claim"`
	// +kubebuilder:validation:XValidation:rule="self.type == 'DirectoryTree' && !has(self.filesystem)",message="state volume requires DirectoryTree without a filesystem"
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

// SandboxTemplateSpec sets the Sandbox's kind: exactly one of Pod and VM.
// +kubebuilder:validation:ExactlyOneOf=pod;vm
// +kubebuilder:validation:XValidation:rule="!has(self.workspaceVolume) || !has(self.pod) || self.workspaceVolume.layout.type == 'DirectoryTree'",message="Pod workspaces require DirectoryTree layout"
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
	// WakeOnSend allows authenticated user and notification sends to resume a stopped Sandbox.
	// Omitted is false so older Sandbox manifests keep wake-on-send disabled.
	// +kubebuilder:default=false
	WakeOnSend bool `json:"wakeOnSend,omitempty"`
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
	// IdleSuspendAt is the controller's next attempt to suspend an idle Sandbox, when one is scheduled.
	IdleSuspendAt *metav1.Time `json:"idleSuspendAt,omitempty"`
	// IdleSuspendBlockers are the live runner facts keeping autosuspend from suspending this Sandbox.
	// +listType=atomic
	IdleSuspendBlockers []AutosuspendBlocker `json:"idleSuspendBlockers,omitempty"`
	// The UID of the last incarnation whose runner sealed every Session, kept after it stops.
	QuiescedIncarnation *string `json:"quiescedIncarnation,omitempty"`
	// The latest Quiesce request, scoped to its runner incarnation, for the controller's retry deadline.
	QuiesceAttempt *QuiesceAttempt `json:"quiesceAttempt,omitempty"`
	// OperatingMode is whether the controller runs the Sandbox: the one derivation every reader of
	// `spec.running` reads instead.
	OperatingMode OperatingMode `json:"operatingMode,omitempty"`
	// SuspendedBy is why the Sandbox is suspended. `Idle` keeps a Sandbox stopped that `spec.running` says to run,
	// and SuspendedAtWakeGeneration is the `spec.wakeGeneration` it stopped it at.
	SuspendedBy *SuspendedBy `json:"suspendedBy,omitempty"`
	// +kubebuilder:validation:Minimum=0
	SuspendedAtWakeGeneration *int64 `json:"suspendedAtWakeGeneration,omitempty"`
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

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
	// +kubebuilder:validation:XValidation:rule="self.type == 'DirectoryTree' && !has(self.filesystem)",message="state volume requires DirectoryTree without a filesystem"
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

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced,path=sandboxes,singular=sandbox
// +kubebuilder:printcolumn:name="Template",type=string,JSONPath=".spec.template"
// +kubebuilder:printcolumn:name="Desired",type=boolean,JSONPath=".spec.running"
// +kubebuilder:printcolumn:name="Running",type=string,JSONPath=".status.conditions[?(@.type==\"Running\")].reason"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
// +kubebuilder:validation:XValidation:rule="size(self.metadata.name) <= 63",message="a Sandbox name is at most 63 characters"
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
