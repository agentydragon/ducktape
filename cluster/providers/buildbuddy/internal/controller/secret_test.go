package controller

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/cluster/providers/buildbuddy/apis/v1alpha1"
	"github.com/agentydragon/ducktape/cluster/providers/buildbuddy/internal/buildbuddy"
	xpv2 "github.com/crossplane/crossplane/apis/v2/core/v2"
	"golang.org/x/crypto/nacl/box"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestSecretReconciliation(t *testing.T) {
	public, _, err := box.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	present, writes := false, 0
	keyID := "fake-key-1"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch strings.TrimPrefix(r.URL.Path, "/") {
		case "ListSecrets":
			var secrets []map[string]string
			if present {
				secrets = append(secrets, map[string]string{"name": "FAKE_KEY"})
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"secret": secrets})
		case "GetPublicKey":
			_ = json.NewEncoder(w).Encode(map[string]any{"publicKey": map[string]string{"id": keyID, "value": base64.StdEncoding.EncodeToString(public[:])}})
		case "UpdateSecret":
			present = true
			writes++
		case "DeleteSecret":
			present = false
		default:
			http.Error(w, "unknown", http.StatusNotFound)
		}
	}))
	defer server.Close()
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	source := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "input", Namespace: "test", UID: types.UID("first"), ResourceVersion: "1"}, Data: map[string][]byte{"value": []byte("fake-value")}}
	kube := fake.NewClientBuilder().WithScheme(scheme).WithObjects(source).Build()
	e := &external{kube: kube, api: &buildbuddy.Client{URL: server.URL + "/", HTTP: server.Client()}}
	cr := &v1alpha1.Secret{ObjectMeta: metav1.ObjectMeta{Namespace: "test", Annotations: map[string]string{"crossplane.io/external-name": "FAKE_KEY"}}, Spec: v1alpha1.SecretSpec{ForProvider: v1alpha1.SecretParameters{ValueSecretRef: xpv2.LocalSecretKeySelector{LocalSecretReference: xpv2.LocalSecretReference{Name: "input"}, Key: "value"}}}}
	ctx := context.Background()
	obs, err := e.Observe(ctx, cr)
	if err != nil || obs.ResourceExists {
		t.Fatalf("initial observation: %+v %v", obs, err)
	}
	if _, err := e.Create(ctx, cr); err != nil {
		t.Fatal(err)
	}
	obs, err = e.Observe(ctx, cr)
	if err != nil || !obs.ResourceExists || obs.ResourceUpToDate {
		t.Fatalf("observation after create: %+v %v", obs, err)
	}
	if _, err := e.Update(ctx, cr); err != nil {
		t.Fatal(err)
	}
	obs, err = e.Observe(ctx, cr)
	if err != nil || !obs.ResourceUpToDate {
		t.Fatalf("observation after update: %+v %v", obs, err)
	}
	status, err := json.Marshal(cr.Status)
	if err != nil || strings.Contains(string(status), "fake-value") {
		t.Fatal("status contains source secret")
	}
	changed := &corev1.Secret{}
	if err := kube.Get(ctx, types.NamespacedName{Name: "input", Namespace: "test"}, changed); err != nil {
		t.Fatal(err)
	}
	changed.Data["value"] = []byte("replacement")
	if err := kube.Update(ctx, changed); err != nil {
		t.Fatal(err)
	}
	obs, err = e.Observe(ctx, cr)
	if err != nil || obs.ResourceUpToDate {
		t.Fatalf("source revision change: %+v %v", obs, err)
	}
	if _, err := e.Update(ctx, cr); err != nil {
		t.Fatal(err)
	}
	keyID = "fake-key-2"
	obs, err = e.Observe(ctx, cr)
	if err != nil || obs.ResourceUpToDate {
		t.Fatalf("public key rotation: %+v %v", obs, err)
	}
	if _, err := e.Update(ctx, cr); err != nil {
		t.Fatal(err)
	}
	old := metav1.NewTime(time.Now().Add(-2 * reassertInterval))
	cr.Status.AtProvider.LastAppliedAt = &old
	obs, err = e.Observe(ctx, cr)
	if err != nil || obs.ResourceUpToDate {
		t.Fatalf("periodic reassertion: %+v %v", obs, err)
	}
	if err := kube.Delete(ctx, source); err != nil {
		t.Fatal(err)
	}
	if _, err := e.Observe(ctx, cr); err == nil || !present {
		t.Fatal("missing source must fail without deleting remote secret")
	}
	if writes != 4 {
		t.Fatalf("got %d writes", writes)
	}
}

func TestSecretReferenceRestrictions(t *testing.T) {
	c := &connector{sourceSecret: "input", credentialSecret: "credentials"}
	cr := &v1alpha1.Secret{Spec: v1alpha1.SecretSpec{ForProvider: v1alpha1.SecretParameters{ValueSecretRef: xpv2.LocalSecretKeySelector{LocalSecretReference: xpv2.LocalSecretReference{Name: "another-secret"}}}}}
	if _, err := c.Connect(context.Background(), cr); err == nil {
		t.Fatal("unexpected access to another source Secret")
	}
	valid := v1alpha1.ProviderCredentials{Source: xpv2.CredentialsSourceSecret, CommonCredentialSelectors: xpv2.CommonCredentialSelectors{SecretRef: &xpv2.SecretKeySelector{SecretReference: xpv2.SecretReference{Name: "credentials", Namespace: "test"}, Key: "api-key"}}}
	if err := c.validateCredentials("test", valid); err != nil {
		t.Fatal(err)
	}
	for _, ref := range []xpv2.SecretReference{{Name: "other", Namespace: "test"}, {Name: "credentials", Namespace: "other"}} {
		bad := valid
		bad.SecretRef = &xpv2.SecretKeySelector{SecretReference: ref, Key: "api-key"}
		if err := c.validateCredentials("test", bad); err == nil {
			t.Fatalf("unexpected access to %+v", ref)
		}
	}
}
