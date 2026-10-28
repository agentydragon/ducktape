package sandboxcontroller

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"slices"
	"strings"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	RunnerTLSVolumeName   = "agentplane-runner-tls"
	RunnerTLSMountPath    = "/run/agentplane/runner-tls"
	RunnerTrustVolumeName = "agentplane-runner-trust"
	RunnerTrustMountPath  = "/run/agentplane/runner-trust"
	RunnerTrustBundleName = "cluster-internal-ca-bundle"
	RunnerTrustBundleKey  = "ca-certificates.crt"

	RunnerCredentialIDAnnotation         = "agentplane.allegedly.works/runner-credential"
	RunnerCredentialKindAnnotation       = "agentplane.allegedly.works/runner-credential-kind"
	RunnerCredentialAcceptedIDAnnotation = "agentplane.allegedly.works/runner-credential-accepted-id"
	RunnerCredentialPodUIDAnnotation     = "agentplane.allegedly.works/runner-credential-pod-uid"
	RunnerCredentialVMIUIDAnnotation     = "agentplane.allegedly.works/runner-credential-vmi-uid"
	RunnerCredentialLastVMIUIDAnnotation = "agentplane.allegedly.works/runner-credential-last-vmi-uid"

	runnerCertificateGroup          = "cert-manager.io"
	runnerCertificateVersion        = "v1"
	runnerCertificateKind           = "Certificate"
	runnerIssuerKind                = "Issuer"
	runnerCertificateNameAnnotation = "cert-manager.io/certificate-name"
	runnerCredentialRetry           = 5 * time.Second
)

var runnerCertificateGVK = schema.GroupVersionKind{
	Group: runnerCertificateGroup, Version: runnerCertificateVersion, Kind: runnerCertificateKind,
}

// RunnerCertificateSecretName is the unique Secret name for one credential generation. The
// random credential ID is intentionally part of the name so renewal can update only this
// generation's Secret and a later Pod or VMI can never inherit a newer identity through a reused
// Secret.
func RunnerCertificateSecretName(credentialID string) string { return "runner-" + credentialID }

func runnerCertificateURI(sandbox *sandboxapi.Sandbox, credentialID string) string {
	return fmt.Sprintf("spiffe://agentplane/runner/%s/%s/%s", sandbox.Namespace, sandbox.UID, credentialID)
}

func newRunnerCredentialID() (string, error) {
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return "", fmt.Errorf("generate runner credential ID: %w", err)
	}
	return hex.EncodeToString(id[:]), nil
}

// ensureRunnerCredential provisions a cert-manager Certificate before starting an incarnation and
// accepts a running VM incarnation only after its VMI UID has been bound to that credential ID.
// Every replacement gets a fresh Secret name; changing a Secret mounted by an old runner could
// otherwise let that process acquire the replacement's identity.
func (r *Reconciler) ensureRunnerCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload) (workload, *unready, error) {
	if w.pod != nil {
		id := w.pod.Labels[sandboxapi.RunnerCredentialLabel]
		if !validRunnerCredentialID(id) {
			// Keep a legacy active incarnation visible to the normal quiesce/stop path. A
			// new Gateway cannot authenticate it without the mTLS credential.
			w.runnerIncarnationPublished = w.pod.UID != ""
			return w, &unready{reason: "RunnerCredentialMissing", message: fmt.Sprintf("Pod %s has no controller-issued runner credential", w.pod.Name)}, nil
		}
		if !validRunnerTransport(w.pod.Labels[sandboxapi.RunnerTransportLabel]) {
			w.runnerIncarnationPublished = w.pod.UID != ""
			return w, &unready{reason: "RunnerTransportMissing", message: fmt.Sprintf("Pod %s has no explicit runner transport", w.pod.Name)}, nil
		}
		boundID := sandbox.Annotations[RunnerCredentialIDAnnotation]
		boundPodUID := sandbox.Annotations[RunnerCredentialPodUIDAnnotation]
		if w.pod.UID == "" {
			return w, credentialPending("RunnerCredentialPending", fmt.Sprintf("Pod %s has no assigned UID yet", w.pod.Name)), nil
		}
		if boundID != id || (boundPodUID != "" && boundPodUID != string(w.pod.UID)) {
			// Never bind an existing certificate generation to another Pod UID. A replacement with
			// a stale template label is deleted; once it is gone, the no-Pod path assigns a fresh ID
			// and Certificate before creating its replacement.
			if err := r.deleteIfLive(ctx, w.pod); err != nil {
				return w, nil, err
			}
			return w, &unready{reason: "RunnerCredentialMismatch", message: fmt.Sprintf("Pod %s has a stale runner credential and is being replaced", w.pod.Name)}, nil
		}
		if boundPodUID == "" {
			ensureAnnotations(sandbox)
			sandbox.Annotations[RunnerCredentialKindAnnotation] = "pod"
			sandbox.Annotations[RunnerCredentialPodUIDAnnotation] = string(w.pod.UID)
			sandbox.Annotations[RunnerCredentialAcceptedIDAnnotation] = id
			if err := r.Update(ctx, sandbox); err != nil {
				return w, nil, err
			}
		}
		blocked, err := r.ensureRunnerCertificate(ctx, sandbox, id)
		w.runnerCredentialAccepted = blocked == nil
		w.runnerIncarnationPublished = w.runnerCredentialAccepted
		return w, blocked, err
	}
	if w.vm != nil {
		return r.ensureVMRunnerCredential(ctx, sandbox, w)
	}
	if w.vmi != nil {
		return w, &unready{reason: "VMIStopping", message: fmt.Sprintf("VMI %s is still stopping", w.vmi.Name)}, nil
	}
	if !sandboxRunning(sandbox) {
		return w, nil, nil
	}
	template, blocked, err := r.template(ctx, sandbox)
	if err != nil || blocked != nil {
		return w, blocked, err
	}
	kind := "pod"
	if template.Spec.VM != nil {
		kind = "vm"
	}
	id := sandbox.Annotations[RunnerCredentialIDAnnotation]
	previousKind := sandbox.Annotations[RunnerCredentialKindAnnotation]
	boundPodUID := sandbox.Annotations[RunnerCredentialPodUIDAnnotation]
	lastVMIUID := sandbox.Annotations[RunnerCredentialLastVMIUIDAnnotation]
	acceptedID := sandbox.Annotations[RunnerCredentialAcceptedIDAnnotation]
	if !validRunnerCredentialID(id) || previousKind != kind ||
		(kind == "pod" && boundPodUID != "") || (kind == "vm" && lastVMIUID != "" && id == acceptedID) {
		id, err = newRunnerCredentialID()
		if err != nil {
			return w, nil, err
		}
		ensureAnnotations(sandbox)
		sandbox.Annotations[RunnerCredentialIDAnnotation] = id
		sandbox.Annotations[RunnerCredentialKindAnnotation] = kind
		delete(sandbox.Annotations, RunnerCredentialPodUIDAnnotation)
		if err := r.Update(ctx, sandbox); err != nil {
			return w, nil, err
		}
	}
	blocked, err = r.ensureRunnerCertificate(ctx, sandbox, id)
	if err != nil || blocked != nil {
		return w, blocked, err
	}
	// There is no Pod or VMI using the older Secret at this point, so old generations can be
	// garbage-collected after cert-manager has created the new one.
	if pending, err := r.deleteOldRunnerCertificates(ctx, sandbox, id); err != nil || pending != nil {
		return w, pending, err
	}
	return w, nil, nil
}

func (r *Reconciler) ensureVMRunnerCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload) (workload, *unready, error) {
	vm := w.vm
	id := vm.Annotations[RunnerCredentialIDAnnotation]
	expectedVMIUID := vm.Annotations[RunnerCredentialVMIUIDAnnotation]
	if !validRunnerCredentialID(id) {
		// Let an existing legacy incarnation finish through the normal quiesce/stop path before
		// installing credentials. Silently halting here could discard an active turn.
		if vmRunning(vm) || w.vmi != nil {
			w.runnerIncarnationPublished = w.vmi != nil && w.vmi.UID != ""
			return w, &unready{reason: "RunnerCredentialRequired", message: "runner mTLS credentials are installed after the legacy VM stops"}, nil
		}
		var err error
		if id, err = newRunnerCredentialID(); err != nil {
			return w, nil, err
		}
		ensureAnnotations(&vm.ObjectMeta)
		vm.Annotations[RunnerCredentialIDAnnotation] = id
		vm.Annotations[RunnerCredentialVMIUIDAnnotation] = ""
		if expectedVMIUID != "" {
			vm.Annotations[RunnerCredentialLastVMIUIDAnnotation] = expectedVMIUID
		}
		if err := r.Update(ctx, vm); err != nil {
			return w, nil, err
		}
		if err := r.setSandboxRunnerCredential(ctx, sandbox, id, "vm"); err != nil {
			return w, nil, err
		}
		expectedVMIUID = ""
	}

	// A stopped VM starts a new VMI incarnation. Rotate its credential only after the previous VMI
	// has gone, keeping the old Secret mounted only in that old guest.
	if sandboxRunning(sandbox) && !vmRunning(vm) && w.vmi == nil && expectedVMIUID != "" {
		var err error
		if id, err = newRunnerCredentialID(); err != nil {
			return w, nil, err
		}
		ensureAnnotations(&vm.ObjectMeta)
		vm.Annotations[RunnerCredentialIDAnnotation] = id
		vm.Annotations[RunnerCredentialVMIUIDAnnotation] = ""
		if err := r.Update(ctx, vm); err != nil {
			return w, nil, err
		}
		if err := r.setSandboxRunnerCredential(ctx, sandbox, id, "vm"); err != nil {
			return w, nil, err
		}
		expectedVMIUID = ""
	}
	certificateGate, err := r.ensureRunnerCertificate(ctx, sandbox, id)
	if err != nil {
		return w, nil, err
	}

	if w.vmi != nil {
		vmiCredentialID := w.vmi.Labels[sandboxapi.RunnerCredentialLabel]
		if !validRunnerTransport(w.vmi.Labels[sandboxapi.RunnerTransportLabel]) {
			return w, &unready{reason: "RunnerTransportMissing", message: fmt.Sprintf("VMI %s has no explicit runner transport", w.vmi.Name)}, nil
		}
		switch {
		case vmiCredentialID != id:
			// A replacement VMI observed during credential rotation may still be terminating with its
			// old template. Keep the VM halted and wait; do not issue a second identity or accept it.
			if vmRunning(vm) {
				if err := r.haltVM(ctx, vm); err != nil {
					return w, nil, err
				}
			}
			if certificateGate != nil {
				return w, certificateGate, nil
			}
			return w, &unready{reason: "RunnerCredentialMismatch", message: fmt.Sprintf("VMI %s does not carry the VM's active runner credential", w.vmi.Name)}, nil
		case expectedVMIUID != "" && expectedVMIUID != string(w.vmi.UID):
			// KubeVirt recreated an unexpected VMI using the old VM template. Stop that replacement,
			// then issue a fresh Secret before starting another one. The last accepted UID stays durable
			// for audit and so the controller never mistakes a replacement for the old incarnation.
			var err error
			if id, err = newRunnerCredentialID(); err != nil {
				return w, nil, err
			}
			ensureAnnotations(&vm.ObjectMeta)
			vm.Annotations[RunnerCredentialLastVMIUIDAnnotation] = expectedVMIUID
			vm.Annotations[RunnerCredentialIDAnnotation] = id
			vm.Annotations[RunnerCredentialVMIUIDAnnotation] = ""
			if vmRunning(vm) {
				halted := kubevirtRunStrategyHalted()
				vm.Spec.Running, vm.Spec.RunStrategy = nil, &halted
			}
			if err := r.Update(ctx, vm); err != nil {
				return w, nil, err
			}
			if err := r.setSandboxRunnerCredential(ctx, sandbox, id, "vm"); err != nil {
				return w, nil, err
			}
			if certificateGate, err = r.ensureRunnerCertificate(ctx, sandbox, id); err != nil || certificateGate != nil {
				return w, certificateGate, err
			}
			return w, &unready{reason: "RunnerCredentialMismatch", message: fmt.Sprintf("VMI %s was replaced; its credential is being rotated", w.vmi.Name)}, nil
		case expectedVMIUID == "":
			if certificateGate != nil {
				return w, certificateGate, nil
			}
			// The first VMI for a credential generation is accepted only when KubeVirt copied the
			// registered credential label from this VM's template.
			ensureAnnotations(&vm.ObjectMeta)
			vm.Annotations[RunnerCredentialVMIUIDAnnotation] = string(w.vmi.UID)
			vm.Annotations[RunnerCredentialLastVMIUIDAnnotation] = string(w.vmi.UID)
			if err := r.Update(ctx, vm); err != nil {
				return w, nil, err
			}
			if err := r.setSandboxLastVMIUID(ctx, sandbox, id, string(w.vmi.UID)); err != nil {
				return w, nil, err
			}
			expectedVMIUID = string(w.vmi.UID)
		}
	}

	if certificateGate != nil {
		return w, certificateGate, nil
	}
	if w.vmi != nil && expectedVMIUID == string(w.vmi.UID) && w.vmi.Labels[sandboxapi.RunnerCredentialLabel] == id {
		w.runnerCredentialAccepted = true
		w.runnerIncarnationPublished = true
		if sandbox.Annotations[RunnerCredentialLastVMIUIDAnnotation] != string(w.vmi.UID) {
			if err := r.setSandboxLastVMIUID(ctx, sandbox, id, string(w.vmi.UID)); err != nil {
				return w, nil, err
			}
		}
	}
	if w.vmi == nil {
		if pending, err := r.deleteOldRunnerCertificates(ctx, sandbox, id); err != nil || pending != nil {
			return w, pending, err
		}
	}
	return w, nil, nil
}

func kubevirtRunStrategyHalted() kubevirtv1.VirtualMachineRunStrategy {
	return kubevirtv1.RunStrategyHalted
}

func (r *Reconciler) setSandboxRunnerCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, id, kind string) error {
	ensureAnnotations(sandbox)
	sandbox.Annotations[RunnerCredentialIDAnnotation] = id
	sandbox.Annotations[RunnerCredentialKindAnnotation] = kind
	delete(sandbox.Annotations, RunnerCredentialPodUIDAnnotation)
	return r.Update(ctx, sandbox)
}

func (r *Reconciler) setSandboxLastVMIUID(ctx context.Context, sandbox *sandboxapi.Sandbox, id, vmiUID string) error {
	ensureAnnotations(sandbox)
	sandbox.Annotations[RunnerCredentialIDAnnotation] = id
	sandbox.Annotations[RunnerCredentialKindAnnotation] = "vm"
	sandbox.Annotations[RunnerCredentialLastVMIUIDAnnotation] = vmiUID
	sandbox.Annotations[RunnerCredentialAcceptedIDAnnotation] = id
	return r.Update(ctx, sandbox)
}

func (r *Reconciler) ensureRunnerCertificate(ctx context.Context, sandbox *sandboxapi.Sandbox, credentialID string) (*unready, error) {
	if r.RunnerCertificateIssuer == "" {
		return &unready{reason: "RunnerCertificateIssuerMissing", message: "runner_certificate_issuer is required"}, nil
	}
	secretName := RunnerCertificateSecretName(credentialID)
	certificate := runnerCertificateObject(sandbox, r.RunnerCertificateIssuer, credentialID)
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(runnerCertificateGVK)
	err := r.Objects.Get(ctx, client.ObjectKey{Namespace: sandbox.Namespace, Name: secretName}, current)
	if apierrors.IsNotFound(err) {
		if err := r.Create(ctx, certificate); err != nil {
			if apierrors.IsAlreadyExists(err) {
				return credentialPending("CertificatePending", fmt.Sprintf("Certificate %s is being created", secretName)), nil
			}
			return nil, err
		}
		return credentialPending("CertificatePending", fmt.Sprintf("Certificate %s is waiting for cert-manager", secretName)), nil
	}
	if err != nil {
		return nil, err
	}
	if !metav1.IsControlledBy(current, sandbox) || !current.GetDeletionTimestamp().IsZero() {
		return &unready{reason: "CertificateConflict", message: fmt.Sprintf("Certificate %s is foreign or being deleted", secretName)}, nil
	}
	currentSpec, found, err := unstructured.NestedMap(current.Object, "spec")
	if err != nil {
		return nil, err
	}
	desiredSpec, _, _ := unstructured.NestedMap(certificate.Object, "spec")
	if !found || !runnerCertificateSpecMatches(currentSpec, desiredSpec) {
		return &unready{reason: "CertificateIdentityConflict", message: fmt.Sprintf("Certificate %s does not have the immutable runner identity", secretName)}, nil
	}
	if !certificateReady(current) {
		return credentialPending("CertificatePending", fmt.Sprintf("Certificate %s is not Ready", secretName)), nil
	}
	var secret corev1.Secret
	if err := r.Objects.Get(ctx, client.ObjectKey{Namespace: sandbox.Namespace, Name: secretName}, &secret); err != nil {
		if apierrors.IsNotFound(err) {
			return credentialPending("CertificateSecretPending", fmt.Sprintf("Secret %s is not available yet", secretName)), nil
		}
		return nil, err
	}
	if secret.Annotations[runnerCertificateNameAnnotation] != secretName ||
		secret.Labels[sandboxapi.SandboxLabel] != sandbox.Name ||
		secret.Labels[sandboxapi.RunnerCredentialLabel] != credentialID || secret.Type != corev1.SecretTypeTLS {
		return &unready{reason: "CertificateSecretConflict", message: fmt.Sprintf("Secret %s is not the cert-manager Secret for this runner identity", secretName)}, nil
	}
	for _, key := range []string{"tls.crt", "tls.key"} {
		if len(secret.Data[key]) == 0 {
			return credentialPending("CertificateSecretPending", fmt.Sprintf("Secret %s is missing %s", secretName, key)), nil
		}
	}
	return nil, nil
}

func runnerCertificateObject(sandbox *sandboxapi.Sandbox, issuer, credentialID string) *unstructured.Unstructured {
	owner := metav1.NewControllerRef(sandbox, sandboxapi.GroupVersion.WithKind("Sandbox"))
	return &unstructured.Unstructured{Object: map[string]interface{}{
		"apiVersion": runnerCertificateGroup + "/" + runnerCertificateVersion,
		"kind":       runnerCertificateKind,
		"metadata": map[string]interface{}{
			"name":      RunnerCertificateSecretName(credentialID),
			"namespace": sandbox.Namespace,
			"labels": map[string]interface{}{
				sandboxapi.SandboxLabel:          sandbox.Name,
				sandboxapi.RunnerCredentialLabel: credentialID,
			},
			"ownerReferences": []interface{}{map[string]interface{}{
				"apiVersion": owner.APIVersion,
				"kind":       owner.Kind,
				"name":       owner.Name,
				"uid":        string(owner.UID),
				"controller": true,
			}},
		},
		"spec": map[string]interface{}{
			"secretName":  RunnerCertificateSecretName(credentialID),
			"duration":    "24h",
			"renewBefore": "8h",
			"issuerRef": map[string]interface{}{
				"name":  issuer,
				"kind":  runnerIssuerKind,
				"group": runnerCertificateGroup,
			},
			"uris":       []interface{}{runnerCertificateURI(sandbox, credentialID)},
			"usages":     []interface{}{"digital signature", "client auth", "server auth"},
			"privateKey": map[string]interface{}{"algorithm": "ECDSA", "size": int64(256), "rotationPolicy": "Always"},
			"secretTemplate": map[string]interface{}{"labels": map[string]interface{}{
				sandboxapi.SandboxLabel:          sandbox.Name,
				sandboxapi.RunnerCredentialLabel: credentialID,
			}},
		},
	}}
}

// cert-manager's webhook may add harmless defaults such as privateKey.encoding and isCA=false.
// Compare the identity and key policy we own while rejecting additional SANs or usages that could
// grant this Secret another workload identity.
func runnerCertificateSpecMatches(actual, expected map[string]interface{}) bool {
	allowedFields := map[string]struct{}{
		"secretName": {}, "duration": {}, "renewBefore": {}, "issuerRef": {}, "uris": {},
		"usages": {}, "privateKey": {}, "secretTemplate": {}, "isCA": {}, "revisionHistoryLimit": {},
	}
	for field := range actual {
		if _, ok := allowedFields[field]; !ok {
			return false
		}
	}
	for _, field := range []string{"secretName", "issuerRef", "uris", "usages", "secretTemplate"} {
		if !equality.Semantic.DeepEqual(actual[field], expected[field]) {
			return false
		}
	}
	for _, field := range []string{"duration", "renewBefore"} {
		actualDuration, actualOK := actual[field].(string)
		expectedDuration, expectedOK := expected[field].(string)
		actualValue, actualErr := time.ParseDuration(actualDuration)
		expectedValue, expectedErr := time.ParseDuration(expectedDuration)
		if !actualOK || !expectedOK || actualErr != nil || expectedErr != nil || actualValue != expectedValue {
			return false
		}
	}
	if isCA, exists := actual["isCA"]; exists && isCA != false {
		return false
	}
	if revisionLimit, exists := actual["revisionHistoryLimit"]; exists {
		if value, ok := revisionLimit.(int64); !ok || value < 1 {
			return false
		}
	}
	actualKey, ok := actual["privateKey"].(map[string]interface{})
	if !ok {
		return false
	}
	expectedKey, ok := expected["privateKey"].(map[string]interface{})
	if !ok {
		return false
	}
	for field := range actualKey {
		if field != "algorithm" && field != "size" && field != "rotationPolicy" && field != "encoding" {
			return false
		}
	}
	for _, field := range []string{"algorithm", "size", "rotationPolicy"} {
		if !equality.Semantic.DeepEqual(actualKey[field], expectedKey[field]) {
			return false
		}
	}
	if encoding, exists := actualKey["encoding"]; exists && encoding != "PKCS1" && encoding != "PKCS8" {
		return false
	}
	return true
}

func certificateReady(certificate *unstructured.Unstructured) bool {
	conditions, found, err := unstructured.NestedSlice(certificate.Object, "status", "conditions")
	if err != nil || !found {
		return false
	}
	for _, item := range conditions {
		condition, ok := item.(map[string]interface{})
		if !ok || condition["type"] != "Ready" || condition["status"] != "True" {
			continue
		}
		return true
	}
	return false
}

func (r *Reconciler) deleteOldRunnerCertificates(ctx context.Context, sandbox *sandboxapi.Sandbox, keepID string) (*unready, error) {
	listCertificates := func() (*unstructured.UnstructuredList, error) {
		certificates := &unstructured.UnstructuredList{}
		certificates.SetGroupVersionKind(schema.GroupVersionKind{Group: runnerCertificateGroup, Version: runnerCertificateVersion, Kind: "CertificateList"})
		err := r.Objects.List(ctx, certificates, client.InNamespace(sandbox.Namespace), client.MatchingLabels{sandboxapi.SandboxLabel: sandbox.Name})
		return certificates, err
	}
	certificates, err := listCertificates()
	if err != nil {
		return nil, err
	}
	for i := range certificates.Items {
		certificate := &certificates.Items[i]
		credentialID := certificate.GetLabels()[sandboxapi.RunnerCredentialLabel]
		if credentialID == keepID || !metav1.IsControlledBy(certificate, sandbox) {
			continue
		}
		if !validRunnerCredentialID(credentialID) || certificate.GetName() != RunnerCertificateSecretName(credentialID) {
			return &unready{reason: "CertificateConflict", message: fmt.Sprintf("refusing to remove unexpected Certificate %s", certificate.GetName())}, nil
		}
		uid := certificate.GetUID()
		if uid == "" {
			err = r.Delete(ctx, certificate)
		} else {
			err = r.Delete(ctx, certificate, client.Preconditions{UID: &uid})
		}
		if err != nil && !apierrors.IsNotFound(err) {
			return nil, err
		}
	}

	// cert-manager does not set Certificate owner references on generated Secrets unless its
	// cluster-wide enable-certificate-owner-ref switch is enabled. Remove only Secrets carrying the
	// labels and certificate-name annotation declared by this Certificate, after the Certificate is
	// gone or its deletion has begun. UID preconditions prevent deleting a Secret recreated by another
	// owner between the read and delete.
	remaining := map[string]*unstructured.Unstructured{}
	certificates, err = listCertificates()
	if err != nil {
		return nil, err
	}
	pending := false
	for i := range certificates.Items {
		certificate := &certificates.Items[i]
		credentialID := certificate.GetLabels()[sandboxapi.RunnerCredentialLabel]
		if credentialID == keepID || !metav1.IsControlledBy(certificate, sandbox) {
			continue
		}
		remaining[certificate.GetName()] = certificate
		pending = true
	}
	secrets := &corev1.SecretList{}
	if err := r.Objects.List(ctx, secrets, client.InNamespace(sandbox.Namespace), client.MatchingLabels{sandboxapi.SandboxLabel: sandbox.Name}); err != nil {
		return nil, err
	}
	for i := range secrets.Items {
		secret := &secrets.Items[i]
		credentialID := secret.Labels[sandboxapi.RunnerCredentialLabel]
		if credentialID == keepID || credentialID == "" {
			continue
		}
		if !validRunnerCredentialID(credentialID) || secret.Annotations[runnerCertificateNameAnnotation] != secret.Name ||
			secret.Name != RunnerCertificateSecretName(credentialID) || secret.Type != corev1.SecretTypeTLS {
			return &unready{reason: "CertificateSecretConflict", message: fmt.Sprintf("refusing to remove unexpected Secret %s", secret.Name)}, nil
		}
		if certificate := remaining[secret.Name]; certificate != nil && certificate.GetDeletionTimestamp().IsZero() {
			continue
		}
		uid := secret.UID
		if uid == "" {
			err = r.Delete(ctx, secret)
		} else {
			err = r.Delete(ctx, secret, client.Preconditions{UID: &uid})
		}
		if err != nil && !apierrors.IsNotFound(err) {
			return nil, err
		}
	}
	if pending {
		return credentialPending("RunnerCredentialCleanupPending", "old runner Certificates are being removed"), nil
	}
	return nil, nil
}

func credentialPending(reason, message string) *unready {
	return &unready{reason: reason, message: message, retryAfter: runnerCredentialRetry}
}

func validRunnerCredentialID(id string) bool {
	if len(id) != 32 || strings.Trim(id, "0123456789abcdef") != "" {
		return false
	}
	return true
}

func validRunnerTransport(transport string) bool {
	return transport == string(sandboxapi.RunnerTransportForward) || transport == string(sandboxapi.RunnerTransportReverse)
}

func withRunnerTLS(sandbox *sandboxapi.Sandbox, template *sandboxapi.SandboxTemplate, spec *corev1.PodSpec, credentialID string) *unready {
	i := slices.IndexFunc(spec.Containers, func(container corev1.Container) bool { return container.Name == RunnerContainer })
	if i < 0 {
		return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s has no %s container", template.Name, RunnerContainer)}
	}
	if slices.ContainsFunc(spec.Volumes, func(volume corev1.Volume) bool { return volume.Name == RunnerTLSVolumeName }) ||
		slices.ContainsFunc(spec.Containers[i].VolumeMounts, func(mount corev1.VolumeMount) bool { return mount.MountPath == RunnerTLSMountPath }) {
		return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s defines the reserved runner TLS volume or mount", template.Name)}
	}
	mode := int32(0440)
	spec.Volumes = append(spec.Volumes, corev1.Volume{
		Name: RunnerTLSVolumeName,
		VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: &mode,
			Sources: []corev1.VolumeProjection{
				{Secret: &corev1.SecretProjection{
					LocalObjectReference: corev1.LocalObjectReference{Name: RunnerCertificateSecretName(credentialID)},
					Items:                []corev1.KeyToPath{{Key: "tls.crt", Path: "tls.crt"}, {Key: "tls.key", Path: "tls.key"}},
				}},
				{ConfigMap: &corev1.ConfigMapProjection{
					LocalObjectReference: corev1.LocalObjectReference{Name: RunnerTrustBundleName},
					Items:                []corev1.KeyToPath{{Key: RunnerTrustBundleKey, Path: "ca.crt"}},
				}},
			},
		}},
	})
	spec.Containers[i].VolumeMounts = append(spec.Containers[i].VolumeMounts, corev1.VolumeMount{
		Name: RunnerTLSVolumeName, MountPath: RunnerTLSMountPath, ReadOnly: true,
	})
	return nil
}

func withRunnerTLSDisk(sandbox *sandboxapi.Sandbox, template *sandboxapi.SandboxTemplate, instance *kubevirtv1.VirtualMachineInstanceSpec, credentialID string) *unready {
	if slices.ContainsFunc(instance.Volumes, func(volume kubevirtv1.Volume) bool {
		return volume.Name == RunnerTLSVolumeName || volume.Name == RunnerTrustVolumeName
	}) || slices.ContainsFunc(instance.Domain.Devices.Filesystems, func(filesystem kubevirtv1.Filesystem) bool {
		return filesystem.Name == RunnerTLSVolumeName || filesystem.Name == RunnerTrustVolumeName
	}) {
		return &unready{reason: "TemplateInvalid", message: fmt.Sprintf("SandboxTemplate %s defines the reserved runner TLS volume", template.Name)}
	}
	instance.Volumes = append(instance.Volumes, kubevirtv1.Volume{
		Name: RunnerTLSVolumeName,
		VolumeSource: kubevirtv1.VolumeSource{Secret: &kubevirtv1.SecretVolumeSource{
			SecretName: RunnerCertificateSecretName(credentialID),
		}},
	})
	instance.Volumes = append(instance.Volumes, kubevirtv1.Volume{
		Name: RunnerTrustVolumeName,
		VolumeSource: kubevirtv1.VolumeSource{ConfigMap: &kubevirtv1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: RunnerTrustBundleName},
		}},
	})
	instance.Domain.Devices.Filesystems = append(instance.Domain.Devices.Filesystems, kubevirtv1.Filesystem{
		Name: RunnerTLSVolumeName, Virtiofs: &kubevirtv1.FilesystemVirtiofs{},
	})
	instance.Domain.Devices.Filesystems = append(instance.Domain.Devices.Filesystems, kubevirtv1.Filesystem{
		Name: RunnerTrustVolumeName, Virtiofs: &kubevirtv1.FilesystemVirtiofs{},
	})
	return nil
}

func ensureAnnotations(meta metav1.Object) {
	if meta.GetAnnotations() == nil {
		meta.SetAnnotations(map[string]string{})
	}
}
