// Package workloadidentitytesting plays the workload-identity issuer and cert-manager for requesters'
// tests on a test API server.
package workloadidentitytesting

import (
	"crypto/x509"
	"os"
	"testing"
	"time"

	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/issuer"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/bazelbuild/rules_go/go/runfiles"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

// certificateCRDRlocation is set by x_defs to cert-manager's Certificate CRD.
var certificateCRDRlocation string

// CertificateCRD installs cert-manager's Certificate CRD, which the issuer writes.
func CertificateCRD() (kubeapiserver.Option, error) {
	path, err := runfiles.Rlocation(certificateCRDRlocation)
	if err != nil {
		return nil, err
	}
	return kubeapiserver.WithCRDs(path), nil
}

// Issuance issues the WorkloadCredentials of one namespace. Objects must know the
// WorkloadCredential kind and read the API server directly.
type Issuance struct {
	Objects   client.Client
	CA        *transporttesting.CA
	Namespace string
}

// Settle runs the Policy Controller's issuer on every record in the namespace until it settles, and
// does what cert-manager would for every Certificate it requested: a key pair from CA for the
// Certificate's URI in its Secret, labelled from secretTemplate, and the Certificate Ready.
func (i Issuance) Settle(t *testing.T) {
	t.Helper()
	ctx := t.Context()
	reconciler := &issuer.Reconciler{Client: i.Objects, APIReader: i.Objects, Issuer: "test-workload-issuer"}
	var records workloadapi.WorkloadCredentialList
	if err := i.Objects.List(ctx, &records, client.InNamespace(i.Namespace)); err != nil {
		t.Fatal(err)
	}
	for _, record := range records.Items {
		// A fresh ID, then its Certificate, then Ready once that is issued.
		for range 3 {
			key := types.NamespacedName{Namespace: record.Namespace, Name: record.Name}
			if _, err := reconciler.Reconcile(ctx, reconcile.Request{NamespacedName: key}); err != nil {
				t.Fatal(err)
			}
			i.issueCertificates(t)
		}
	}
}

func (i Issuance) issueCertificates(t *testing.T) {
	t.Helper()
	ctx := t.Context()
	certificates := &unstructured.UnstructuredList{}
	certificates.SetGroupVersionKind(issuer.NewCertificate().GroupVersionKind().GroupVersion().WithKind("CertificateList"))
	if err := i.Objects.List(ctx, certificates, client.InNamespace(i.Namespace)); err != nil {
		t.Fatal(err)
	}
	for n := range certificates.Items {
		certificate := &certificates.Items[n]
		if conditions, _, _ := unstructured.NestedSlice(certificate.Object, "status", "conditions"); len(conditions) > 0 ||
			!certificate.GetDeletionTimestamp().IsZero() {
			continue
		}
		name, _, _ := unstructured.NestedString(certificate.Object, "spec", "secretName")
		labels, _, _ := unstructured.NestedStringMap(certificate.Object, "spec", "secretTemplate", "labels")
		uris, _, _ := unstructured.NestedStringSlice(certificate.Object, "spec", "uris")
		issued := i.CA.IssueWithUsage(t, uris[0], x509.ExtKeyUsageClientAuth, x509.ExtKeyUsageServerAuth)
		pair := map[string][]byte{}
		for key, file := range map[string]string{corev1.TLSCertKey: issued.CertFile, corev1.TLSPrivateKeyKey: issued.KeyFile} {
			content, err := os.ReadFile(file)
			if err != nil {
				t.Fatal(err)
			}
			pair[key] = content
		}
		secret := &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{
				Namespace: i.Namespace, Name: name, Labels: labels,
				Annotations: map[string]string{"cert-manager.io/certificate-name": certificate.GetName()},
			},
			Type: corev1.SecretTypeTLS, Data: pair,
		}
		if err := i.Objects.Create(ctx, secret); apierrors.IsAlreadyExists(err) {
			if err := i.Objects.Update(ctx, secret); err != nil {
				t.Fatal(err)
			}
		} else if err != nil {
			t.Fatal(err)
		}
		if err := unstructured.SetNestedSlice(certificate.Object, []interface{}{map[string]interface{}{
			"type": "Ready", "status": "True", "reason": "Ready", "message": "test issued",
			"lastTransitionTime": time.Now().UTC().Format(time.RFC3339),
		}}, "status", "conditions"); err != nil {
			t.Fatal(err)
		}
		if err := i.Objects.Status().Update(ctx, certificate); err != nil {
			t.Fatal(err)
		}
	}
}

// While returns work's result, settling issuance until work returns: work is a requester waiting for
// its credential, as the issuer and cert-manager would serve it in a cluster.
func While[T any](t *testing.T, issuance Issuance, work func() T) T {
	t.Helper()
	done := make(chan T, 1)
	go func() { done <- work() }()
	for {
		select {
		case result := <-done:
			return result
		case <-time.After(50 * time.Millisecond):
			issuance.Settle(t)
		}
	}
}
