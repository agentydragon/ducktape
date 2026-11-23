package sandboxcontroller

import (
	"context"
	"encoding/json"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func getLaunch(t *testing.T, c client.Client) (*corev1.ConfigMap, launch) {
	t.Helper()
	configMap := getOrNil[corev1.ConfigMap](t, c, client.ObjectKey{Namespace: namespace, Name: LaunchConfigMapName(sandboxName)})
	if configMap == nil {
		return nil, launch{}
	}
	var parsed launch
	if err := json.Unmarshal([]byte(configMap.Data[LaunchFile]), &parsed); err != nil {
		t.Fatal(err)
	}
	return configMap, parsed
}

func setBootstrap(t *testing.T, c client.Client, bootstrap string) {
	t.Helper()
	sandbox := getSandbox(t, c)
	sandbox.Spec.Bootstrap = bootstrap
	if err := c.Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
}

func TestRuntimeContainerMountsTheSandboxsLaunchFile(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	setBootstrap(t, c, "echo test-bootstrap")
	reconcileOnce(t, r)

	configMap, parsed := getLaunch(t, c)
	if configMap == nil || !metav1.IsControlledBy(configMap, getSandbox(t, c)) {
		t.Fatalf("launch ConfigMap = %v, want one the Sandbox controls", configMap)
	}
	want := launch{Sandbox: launchSandbox{Namespace: namespace, Name: sandboxName, UID: "uid-sandbox"}, Bootstrap: "echo test-bootstrap"}
	if parsed != want {
		t.Errorf("launch = %+v, want %+v", parsed, want)
	}
	pod := getPod(t, c)
	if pod == nil {
		t.Fatal("no Pod")
	}
	var mounted bool
	for _, mount := range pod.Spec.Containers[0].VolumeMounts {
		mounted = mounted || mount.MountPath == LaunchMountPath && mount.ReadOnly
	}
	if !mounted {
		t.Errorf("Environment Runtime container mounts = %v, want the launch ConfigMap at %s", pod.Spec.Containers[0].VolumeMounts, LaunchMountPath)
	}
	var found bool
	for _, volume := range pod.Spec.Volumes {
		found = found || volume.ConfigMap != nil && volume.ConfigMap.Name == LaunchConfigMapName(sandboxName)
	}
	if !found {
		t.Errorf("Pod volumes = %v, want the launch ConfigMap", pod.Spec.Volumes)
	}
}

func TestSandboxWithoutBootstrapLaunchesWithoutOne(t *testing.T) {
	r, c := newReconciler(t, false, testTemplate("test-image:v1"))
	reconcileOnce(t, r)

	if _, parsed := getLaunch(t, c); parsed.Bootstrap != "" {
		t.Errorf("launch bootstrap = %q, want none", parsed.Bootstrap)
	}
}

func TestChangedBootstrapReachesTheLaunchFileAndKeepsTheRunningPod(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	setBootstrap(t, c, "echo first")
	reconcileOnce(t, r)
	pod := getPod(t, c)

	setBootstrap(t, c, "echo second")
	reconcileOnce(t, r)

	if _, parsed := getLaunch(t, c); parsed.Bootstrap != "echo second" {
		t.Errorf("launch bootstrap = %q, want the changed one", parsed.Bootstrap)
	}
	if again := getPod(t, c); again.UID != pod.UID {
		t.Error("changing the bootstrap replaced the running Pod")
	}
}

func TestVMGetsTheLaunchFileAsADisk(t *testing.T) {
	r, c := newReconciler(t, true, testVMTemplate("test-guest:v1"))
	reconcileOnce(t, r)

	vm := getVM(t, c)
	if vm == nil {
		t.Fatal("no VM")
	}
	volume := volume(vm, launchVolume)
	if volume == nil || volume.ConfigMap == nil || volume.ConfigMap.Name != LaunchConfigMapName(sandboxName) {
		t.Fatalf("VM launch volume = %v, want the launch ConfigMap", volume)
	}
	var serial string
	for _, disk := range vm.Spec.Template.Spec.Domain.Devices.Disks {
		if disk.Name == launchVolume {
			serial = disk.Serial
		}
	}
	if serial != LaunchDiskSerial {
		t.Errorf("launch disk serial = %q, want %q", serial, LaunchDiskSerial)
	}
}
