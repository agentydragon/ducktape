// Package issuer is the workload-identity component's reconciler, hosted in the Policy Controller.
// It is the only writer of workload Certificates and of WorkloadCredential status: it mints each
// record's credential ID and URI, has cert-manager issue them, and reports the result. It reads
// owner references as data and never reads an owner.
package issuer

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"

	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/selection"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	// RecordUIDLabel names the WorkloadCredential a Certificate and its Secret were issued for.
	RecordUIDLabel = "agentplane.allegedly.works/workload-credential-uid"
	// CredentialIDLabel is the credential ID a Certificate and its Secret carry.
	CredentialIDLabel = "agentplane.allegedly.works/workload-credential-id"

	retry = 5 * time.Second
)

// Reconciler issues the WorkloadCredentials its manager's cache holds.
type Reconciler struct {
	// Client reads records and Certificates from the cache and writes through the API server.
	Client client.Client
	// APIReader reads Secrets, which the cache does not hold, and lists by label for cleanup.
	APIReader client.Reader
	// Issuer is the cert-manager ClusterIssuer that signs workload certificates.
	Issuer string
}

// SetupWithManager registers the reconciler: a Certificate it owns maps back to its record.
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	if r.Client == nil || r.APIReader == nil || r.Issuer == "" {
		return errors.New("the WorkloadCredential issuer needs a client, an API reader and an issuer")
	}
	return ctrl.NewControllerManagedBy(mgr).
		Named("workloadcredential").
		For(&workloadapi.WorkloadCredential{}).
		Owns(NewCertificate()).
		Complete(r)
}

// outcome is the Ready condition a reconcile ends with.
type outcome struct {
	ready           bool
	reason, message string
}

func (r *Reconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	var record workloadapi.WorkloadCredential
	if err := r.Client.Get(ctx, request.NamespacedName, &record); apierrors.IsNotFound(err) {
		return r.sweep(ctx, request.Namespace)
	} else if err != nil {
		return ctrl.Result{}, err
	}
	if !record.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}

	status := *record.Status.DeepCopy()
	// A credential ID lives exactly as long as the binding it was accepted for.
	if !validCredentialID(status.CredentialID) || (status.AcceptedBinding != nil && !sameBinding(record.Spec.Binding, status.AcceptedBinding)) {
		id, err := newCredentialID()
		if err != nil {
			return ctrl.Result{}, err
		}
		status.CredentialID, status.AcceptedBinding = id, nil
	}
	// The first binding after a fresh ID accepts it: the incarnation was started with it.
	if status.AcceptedBinding == nil && record.Spec.Binding != nil {
		status.AcceptedBinding = record.Spec.Binding.DeepCopy()
	}
	uri, err := transport.WorkloadID{Namespace: record.Namespace, Name: record.Name, CredentialID: status.CredentialID}.URI()
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("WorkloadCredential %s: %w", request.NamespacedName, err)
	}
	status.URI, status.SecretName = uri, secretName(&record, status.CredentialID)
	status.ObservedGeneration = record.Generation
	// Record a fresh ID before its Certificate exists, so no issued certificate carries an ID the
	// status does not.
	if status.CredentialID != record.Status.CredentialID || !sameBinding(status.AcceptedBinding, record.Status.AcceptedBinding) {
		meta.SetStatusCondition(&status.Conditions, condition(&record, outcome{reason: "CertificatePending", message: "a credential ID was issued"}))
		return r.writeStatus(ctx, &record, status, ctrl.Result{})
	}

	result, err := r.ensureCertificate(ctx, &record, status)
	if err != nil {
		return ctrl.Result{}, err
	}
	requeue := ctrl.Result{}
	if !result.ready {
		requeue.RequeueAfter = retry
	} else if pending, err := r.deleteSuperseded(ctx, &record, status.SecretName); err != nil {
		return ctrl.Result{}, err
	} else if pending {
		requeue.RequeueAfter = retry
	}
	meta.SetStatusCondition(&status.Conditions, condition(&record, result))
	return r.writeStatus(ctx, &record, status, requeue)
}

func (r *Reconciler) writeStatus(ctx context.Context, record *workloadapi.WorkloadCredential, status workloadapi.WorkloadCredentialStatus, result ctrl.Result) (ctrl.Result, error) {
	if equality.Semantic.DeepEqual(record.Status, status) {
		return result, nil
	}
	record.Status = status
	if err := r.Client.Status().Update(ctx, record); err != nil {
		if apierrors.IsConflict(err) {
			return ctrl.Result{Requeue: true}, nil
		}
		return ctrl.Result{}, err
	}
	return result, nil
}

func condition(record *workloadapi.WorkloadCredential, result outcome) metav1.Condition {
	status := metav1.ConditionFalse
	if result.ready {
		status = metav1.ConditionTrue
	}
	return metav1.Condition{
		Type: workloadapi.ConditionReady, Status: status, Reason: result.reason, Message: result.message,
		ObservedGeneration: record.Generation,
	}
}

// The Secret belongs to the credential ID, including while its first Pod binding is accepted.
// An initially ownerless, unbound record gets the stable name a standing Deployment needs;
// rotating its ID later always selects a fresh name, even when the binding was cleared.
func secretName(record *workloadapi.WorkloadCredential, id string) string {
	if id == record.Status.CredentialID && record.Status.SecretName != "" {
		return record.Status.SecretName
	}
	if record.Status.CredentialID == "" && metav1.GetControllerOfNoCopy(record) == nil && record.Spec.Binding == nil {
		return "svid-" + record.Name
	}
	return "svid-" + record.Name + "-" + id
}

func (r *Reconciler) ensureCertificate(ctx context.Context, record *workloadapi.WorkloadCredential, status workloadapi.WorkloadCredentialStatus) (outcome, error) {
	name := status.SecretName
	desired := certificateObject(record, r.Issuer, name, status.URI, status.CredentialID)
	current := NewCertificate()
	err := r.Client.Get(ctx, types.NamespacedName{Namespace: record.Namespace, Name: name}, current)
	if apierrors.IsNotFound(err) {
		if err := r.Client.Create(ctx, desired); err != nil && !apierrors.IsAlreadyExists(err) {
			return outcome{}, fmt.Errorf("create Certificate %s/%s: %w", record.Namespace, name, err)
		}
		return outcome{reason: "CertificatePending", message: fmt.Sprintf("Certificate %s is waiting for cert-manager", name)}, nil
	}
	if err != nil {
		return outcome{}, err
	}
	if !current.GetDeletionTimestamp().IsZero() {
		return outcome{reason: "CertificatePending", message: fmt.Sprintf("Certificate %s is being deleted", name)}, nil
	}
	if !metav1.IsControlledBy(current, record) {
		if controller := metav1.GetControllerOfNoCopy(current); controller != nil && controller.Kind == workloadapi.Kind &&
			controller.APIVersion == workloadapi.SchemeGroupVersion.String() && controller.Name == record.Name {
			// An earlier record of this name is gone, and the garbage collector has yet to remove
			// its Certificate.
			uid := current.GetUID()
			if err := r.Client.Delete(ctx, current, client.Preconditions{UID: &uid}); err != nil && !apierrors.IsNotFound(err) {
				return outcome{}, err
			}
			return outcome{reason: "CertificatePending", message: fmt.Sprintf("Certificate %s belonged to an earlier record and is being deleted", name)}, nil
		}
		return outcome{reason: "CertificateConflict", message: fmt.Sprintf("Certificate %s is not this record's", name)}, nil
	}
	currentSpec, _, err := unstructured.NestedMap(current.Object, "spec")
	if err != nil {
		return outcome{}, err
	}
	desiredSpec, _, _ := unstructured.NestedMap(desired.Object, "spec")
	if !certificateSpecMatches(currentSpec, desiredSpec) {
		return outcome{reason: "CertificateIdentityConflict", message: fmt.Sprintf("Certificate %s does not carry this record's identity", name)}, nil
	}
	if !certificateReady(current) {
		return outcome{reason: "CertificatePending", message: fmt.Sprintf("Certificate %s is not Ready", name)}, nil
	}
	var secret corev1.Secret
	if err := r.APIReader.Get(ctx, types.NamespacedName{Namespace: record.Namespace, Name: name}, &secret); apierrors.IsNotFound(err) {
		return outcome{reason: "CertificateSecretPending", message: fmt.Sprintf("Secret %s is not available yet", name)}, nil
	} else if err != nil {
		return outcome{}, err
	}
	if secret.Annotations[certificateNameAnnotation] != name || secret.Labels[RecordUIDLabel] != string(record.UID) ||
		secret.Labels[CredentialIDLabel] != status.CredentialID || secret.Type != corev1.SecretTypeTLS {
		return outcome{reason: "CertificateSecretPending", message: fmt.Sprintf("Secret %s is not yet cert-manager's Secret for this credential", name)}, nil
	}
	for _, key := range []string{corev1.TLSCertKey, corev1.TLSPrivateKeyKey} {
		if len(secret.Data[key]) == 0 {
			return outcome{reason: "CertificateSecretPending", message: fmt.Sprintf("Secret %s is missing %s", name, key)}, nil
		}
	}
	return outcome{ready: true, reason: "Issued", message: fmt.Sprintf("Secret %s holds credential %s", name, status.CredentialID)}, nil
}

// deleteSuperseded removes the record's earlier generations once the current one is issued:
// Certificates first, then each Secret whose Certificate is gone or going. It reports whether any
// remain.
func (r *Reconciler) deleteSuperseded(ctx context.Context, record *workloadapi.WorkloadCredential, keep string) (bool, error) {
	byRecord := client.MatchingLabels{RecordUIDLabel: string(record.UID)}
	certificates := &unstructured.UnstructuredList{}
	certificates.SetGroupVersionKind(certificateListGVK)
	if err := r.APIReader.List(ctx, certificates, client.InNamespace(record.Namespace), byRecord); err != nil {
		return false, err
	}
	remaining := map[string]bool{}
	for i := range certificates.Items {
		certificate := &certificates.Items[i]
		if certificate.GetName() == keep || !metav1.IsControlledBy(certificate, record) {
			continue
		}
		remaining[certificate.GetName()] = true
		if certificate.GetDeletionTimestamp().IsZero() {
			uid := certificate.GetUID()
			if err := r.Client.Delete(ctx, certificate, client.Preconditions{UID: &uid}); err != nil && !apierrors.IsNotFound(err) {
				return false, err
			}
		}
	}
	secrets := &corev1.SecretList{}
	if err := r.APIReader.List(ctx, secrets, client.InNamespace(record.Namespace), byRecord); err != nil {
		return false, err
	}
	pending := len(remaining) > 0
	for i := range secrets.Items {
		secret := &secrets.Items[i]
		if secret.Name == keep {
			continue
		}
		pending = true
		if err := r.deleteIssuedSecret(ctx, secret); err != nil {
			return false, err
		}
	}
	return pending, nil
}

// sweep removes the Secrets of namespace's records that are gone, once their Certificate is: the
// garbage collector removes the Certificate, and nothing else would remove the Secret.
func (r *Reconciler) sweep(ctx context.Context, namespace string) (ctrl.Result, error) {
	var records workloadapi.WorkloadCredentialList
	if err := r.Client.List(ctx, &records, client.InNamespace(namespace)); err != nil {
		return ctrl.Result{}, err
	}
	live := map[types.UID]bool{}
	for i := range records.Items {
		live[records.Items[i].UID] = true
	}
	issued, err := labels.NewRequirement(RecordUIDLabel, selection.Exists, nil)
	if err != nil {
		return ctrl.Result{}, err
	}
	secrets := &corev1.SecretList{}
	if err := r.APIReader.List(ctx, secrets, client.InNamespace(namespace), client.MatchingLabelsSelector{Selector: labels.NewSelector().Add(*issued)}); err != nil {
		return ctrl.Result{}, err
	}
	pending := false
	for i := range secrets.Items {
		secret := &secrets.Items[i]
		if live[types.UID(secret.Labels[RecordUIDLabel])] {
			continue
		}
		certificate := NewCertificate()
		err := r.APIReader.Get(ctx, client.ObjectKeyFromObject(secret), certificate)
		if err == nil && certificate.GetDeletionTimestamp().IsZero() {
			// Its Certificate would reissue it: wait for the garbage collector.
			pending = true
			continue
		}
		if err != nil && !apierrors.IsNotFound(err) {
			return ctrl.Result{}, err
		}
		if err := r.deleteIssuedSecret(ctx, secret); err != nil {
			return ctrl.Result{}, err
		}
	}
	if pending {
		return ctrl.Result{RequeueAfter: retry}, nil
	}
	return ctrl.Result{}, nil
}

// deleteIssuedSecret deletes a Secret cert-manager issued for a workload Certificate, refusing
// anything else that carries the label. The UID precondition keeps a Secret recreated between the
// read and the delete.
func (r *Reconciler) deleteIssuedSecret(ctx context.Context, secret *corev1.Secret) error {
	if secret.Annotations[certificateNameAnnotation] != secret.Name || secret.Type != corev1.SecretTypeTLS || !strings.HasPrefix(secret.Name, "svid-") {
		return nil
	}
	uid := secret.UID
	if err := r.Client.Delete(ctx, secret, client.Preconditions{UID: &uid}); err != nil && !apierrors.IsNotFound(err) {
		return err
	}
	return nil
}

func sameBinding(a, b *workloadapi.Binding) bool {
	return (a == nil) == (b == nil) && (a == nil || *a == *b)
}

func newCredentialID() (string, error) {
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return "", fmt.Errorf("generate a credential ID: %w", err)
	}
	return hex.EncodeToString(id[:]), nil
}

func validCredentialID(id string) bool {
	return len(id) == 32 && strings.Trim(id, "0123456789abcdef") == ""
}
