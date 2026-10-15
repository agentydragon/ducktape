package sandboxcontroller

import (
	"context"
	"encoding/json"
	"errors"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

func TestStateVolumeCreationRecordsLayoutAndBindsIdentity(t *testing.T) {
	template := testTemplate("test-image:v1")
	r, c := newReconciler(t, true, template)
	reconcileOnce(t, r)
	pvc := getPVC(t, c)
	var record stateVolumeRecord
	if err := json.Unmarshal([]byte(pvc.Annotations[StateVolumeRecordAnnotation]), &record); err != nil {
		t.Fatal(err)
	}
	want := VolumeLayout{Type: DirectoryTree}
	if !equality.Semantic.DeepEqual(record.Layout, want) {
		t.Fatalf("recorded layout = %#v, want %#v", record.Layout, want)
	}
	if got := getSandbox(t, c).Status.Volumes.State; got.Name != pvc.Name || got.UID != string(pvc.UID) || got.Layout != want {
		t.Fatalf("state binding = %#v, want PVC identity and %#v", got, want)
	}
	if getPod(t, c) == nil {
		t.Fatal("recorded DirectoryTree state did not reach the Pod")
	}
}

func TestStateVolumeRejectsUnrecordedOwnedClaimWithoutReplacingData(t *testing.T) {
	template := testTemplate("test-image:v1")
	r, c := newReconciler(t, true, template)
	sandbox := getSandbox(t, c)
	legacy := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: namespace,
			Name:      StateVolumeName(sandbox.Name),
			Labels:    map[string]string{SandboxLabel: sandbox.Name},
		},
		Spec: *template.Spec.StateVolume.Claim.DeepCopy(),
	}
	if err := controllerutil.SetControllerReference(sandbox, legacy, r.Scheme(), controllerutil.WithBlockOwnerDeletion(false)); err != nil {
		t.Fatal(err)
	}
	if err := c.Create(context.Background(), legacy); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)

	retained := getPVC(t, c)
	if retained.UID != legacy.UID {
		t.Fatalf("legacy PVC UID changed from %q to %q", legacy.UID, retained.UID)
	}
	if _, recorded := retained.Annotations[StateVolumeRecordAnnotation]; recorded {
		t.Fatal("unrecorded retained state was silently adopted")
	}
	if getSandbox(t, c).Status.Volumes != nil || getPod(t, c) != nil || runningReason(t, c) != "VolumeLayoutMismatch" {
		t.Fatalf("unrecorded state was attached: status=%#v pod=%v", getSandbox(t, c).Status.Volumes, getPod(t, c))
	}
}

func TestStateVolumeRestartRecoversCreationRecordBeforeStarting(t *testing.T) {
	template := testTemplate("test-image:v1")
	r, c := newReconciler(t, true, template)
	failure := errors.New("controller interrupted before binding persistence")
	r.Client = interceptor.NewClient(c.(client.WithWatch), interceptor.Funcs{
		SubResourceUpdate: func(ctx context.Context, c client.Client, name string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
			if sandbox, ok := obj.(*Sandbox); ok && sandbox.Status.Volumes != nil {
				return failure
			}
			return c.SubResource(name).Update(ctx, obj, opts...)
		},
	})
	if _, err := r.Reconcile(context.Background(), request); !errors.Is(err, failure) {
		t.Fatalf("error = %v, want status persistence failure", err)
	}
	pvc := getPVC(t, c)
	if pvc == nil || pvc.Annotations[StateVolumeRecordAnnotation] == "" || getPod(t, c) != nil || getSandbox(t, c).Status.Volumes != nil {
		t.Fatal("PVC record and binding-before-workload ordering was not preserved")
	}
	restarted := *r
	restarted.Client = c
	reconcileOnce(t, &restarted)
	if got := getSandbox(t, c).Status.Volumes.State; got.UID != string(pvc.UID) || got.Layout.Type != DirectoryTree {
		t.Fatalf("recovered state binding = %#v", got)
	}
	if getPod(t, c) == nil {
		t.Fatal("recorded state claim did not recover")
	}
}

func TestStateVolumeMismatchRetainsPVCAndRefusesResume(t *testing.T) {
	template := testTemplate("test-image:v1")
	r, c := newReconciler(t, true, template)
	reconcileOnce(t, r)
	retained := getPVC(t, c)
	setRunning(t, c, false)
	reconcileOnce(t, r)

	template.Spec.StateVolume.Layout = VolumeLayout{Type: RawDiskImage, Filesystem: "ext4"}
	replaceTemplate(t, c, template)
	setRunning(t, c, true)
	reconcileOnce(t, r)

	if got := getPVC(t, c); got.UID != retained.UID || got.Annotations[StateVolumeRecordAnnotation] != retained.Annotations[StateVolumeRecordAnnotation] {
		t.Fatal("layout mismatch replaced or rewrote the retained state PVC")
	}
	if getPod(t, c) != nil || runningReason(t, c) != "VolumeLayoutMismatch" {
		t.Fatalf("resumed with incompatible state layout: Pod=%v reason=%s", getPod(t, c), runningReason(t, c))
	}
}

func TestStateVolumeIdentityAndRecordMismatchesFailClosed(t *testing.T) {
	for _, change := range []string{"replacement UID", "lost record", "unsupported record"} {
		t.Run(change, func(t *testing.T) {
			r, c := newReconciler(t, true, testTemplate("test-image:v1"))
			reconcileOnce(t, r)
			setRunning(t, c, false)
			reconcileOnce(t, r)
			pvc := getPVC(t, c)
			switch change {
			case "replacement UID":
				if err := c.Delete(context.Background(), pvc); err != nil {
					t.Fatal(err)
				}
				pvc.ResourceVersion = ""
				pvc.UID = types.UID("uid-replacement")
				if err := c.Create(context.Background(), pvc); err != nil {
					t.Fatal(err)
				}
			case "lost record":
				delete(pvc.Annotations, StateVolumeRecordAnnotation)
				if err := c.Update(context.Background(), pvc); err != nil {
					t.Fatal(err)
				}
			case "unsupported record":
				pvc.Annotations[StateVolumeRecordAnnotation] = `{"layout":{"type":"RawDiskImage","filesystem":"ext4"}}`
				if err := c.Update(context.Background(), pvc); err != nil {
					t.Fatal(err)
				}
			}
			setRunning(t, c, true)
			reconcileOnce(t, r)
			if getPod(t, c) != nil || runningReason(t, c) != "VolumeLayoutMismatch" {
				t.Fatalf("mismatch started a workload: Pod=%v reason=%s", getPod(t, c), runningReason(t, c))
			}
		})
	}
}

func TestStateVolumeRejectsUnsupportedLayoutDeclarations(t *testing.T) {
	for _, change := range []string{"omitted layout", "raw disk image", "block filesystem", "filesystem on DirectoryTree", "Block claim"} {
		t.Run(change, func(t *testing.T) {
			template := testTemplate("test-image:v1")
			switch change {
			case "omitted layout":
				template.Spec.StateVolume.Layout = VolumeLayout{}
			case "raw disk image":
				template.Spec.StateVolume.Layout = VolumeLayout{Type: RawDiskImage, Filesystem: "ext4"}
			case "block filesystem":
				template.Spec.StateVolume.Layout = VolumeLayout{Type: BlockFilesystem, Filesystem: "ext4"}
			case "filesystem on DirectoryTree":
				template.Spec.StateVolume.Layout = VolumeLayout{Type: DirectoryTree, Filesystem: "ext4"}
			case "Block claim":
				mode := corev1.PersistentVolumeBlock
				template.Spec.StateVolume.Claim.VolumeMode = &mode
			}
			r, c := newReconciler(t, true, template)
			reconcileOnce(t, r)
			if getPVC(t, c) != nil || getPod(t, c) != nil || runningReason(t, c) != "VolumeLayoutMismatch" {
				t.Fatalf("unsupported layout was accepted: PVC=%v Pod=%v reason=%s", getPVC(t, c), getPod(t, c), runningReason(t, c))
			}
		})
	}
}
