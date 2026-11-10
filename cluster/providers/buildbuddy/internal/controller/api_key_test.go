package controller

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/cluster/providers/buildbuddy/apis/v1alpha1"
	"github.com/agentydragon/ducktape/cluster/providers/buildbuddy/internal/buildbuddy"
	"github.com/crossplane/crossplane-runtime/v2/pkg/meta"
	"github.com/crossplane/crossplane-runtime/v2/pkg/reconciler/managed"
	xpv2 "github.com/crossplane/crossplane/apis/v2/core/v2"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/manager"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

type fakeAPIKeyRecord struct {
	key     buildbuddy.APIKey
	groupID string
	value   []byte
}

type fakeBuildBuddyAPIKey struct {
	keys            map[string]fakeAPIKeyRecord
	createCalls     int
	updateCalls     int
	deleteCalls     int
	createErr       error
	commitBeforeErr bool
	readbackErr     error
}

func (f *fakeBuildBuddyAPIKey) APIKeys(_ context.Context, groupID string) ([]buildbuddy.APIKey, error) {
	keys := make([]buildbuddy.APIKey, 0, len(f.keys))
	for _, record := range f.keys {
		if record.groupID == groupID {
			keys = append(keys, record.key)
		}
	}
	return keys, nil
}

func (f *fakeBuildBuddyAPIKey) APIKeyValue(_ context.Context, groupID, id string) ([]byte, error) {
	if f.readbackErr != nil {
		return nil, f.readbackErr
	}
	record, ok := f.keys[id]
	if !ok || record.groupID != groupID {
		return nil, errors.New("key not found in group")
	}
	return append([]byte(nil), record.value...), nil
}

func (f *fakeBuildBuddyAPIKey) CreateAPIKey(_ context.Context, groupID, label string, capabilities []string, visible bool) (string, error) {
	f.createCalls++
	if f.createErr != nil {
		if f.commitBeforeErr {
			key := buildbuddy.APIKey{ID: "fake-api-key-id", Label: label, Capabilities: append([]string(nil), capabilities...), VisibleToDevelopers: visible}
			f.keys[key.ID] = fakeAPIKeyRecord{key: key, groupID: groupID, value: []byte("fake-generated-key")}
		}
		return "", f.createErr
	}
	key := buildbuddy.APIKey{ID: "fake-api-key-id", Label: label, Capabilities: append([]string(nil), capabilities...), VisibleToDevelopers: visible}
	f.keys[key.ID] = fakeAPIKeyRecord{key: key, groupID: groupID, value: []byte("fake-generated-key")}
	return key.ID, nil
}

func (f *fakeBuildBuddyAPIKey) UpdateAPIKey(_ context.Context, id, label string, capabilities []string, visible bool) error {
	f.updateCalls++
	record := f.keys[id]
	record.key.Label = label
	record.key.Capabilities = append([]string(nil), capabilities...)
	record.key.VisibleToDevelopers = visible
	f.keys[id] = record
	return nil
}

func (f *fakeBuildBuddyAPIKey) DeleteAPIKey(_ context.Context, id string) error {
	f.deleteCalls++
	delete(f.keys, id)
	return nil
}

func (*fakeBuildBuddyAPIKey) Close() error { return nil }

type apiKeyTestConnector struct {
	external *apiKeyExternal
}

func (c *apiKeyTestConnector) Connect(context.Context, *v1alpha1.APIKey) (managed.TypedExternalClient[*v1alpha1.APIKey], error) {
	return c.external, nil
}

type apiKeyTestManager struct {
	manager.Manager
	kube   client.Client
	scheme *runtime.Scheme
}

func (m *apiKeyTestManager) GetClient() client.Client   { return m.kube }
func (m *apiKeyTestManager) GetScheme() *runtime.Scheme { return m.scheme }

func TestAPIKeyReconciliation(t *testing.T) {
	api := &fakeBuildBuddyAPIKey{keys: make(map[string]fakeAPIKeyRecord)}
	kube := newAPIKeyTestClient(t)
	e := &apiKeyExternal{kube: kube, api: api}
	cr := newAPIKeyResource()
	ctx := context.Background()

	creation, err := e.Create(ctx, cr)
	if err != nil {
		t.Fatal("create API key:", err)
	}
	if meta.GetExternalName(cr) != "fake-api-key-id" || string(creation.ConnectionDetails[apiKeyConnectionSecretKey]) != "fake-generated-key" {
		t.Fatal("create did not set the ID and read back the connection detail")
	}
	status, err := json.Marshal(cr.Status)
	if err != nil || strings.Contains(string(status), "fake-generated-key") {
		t.Fatal("API-key value was stored in managed-resource status")
	}
	if api.createCalls != 1 {
		t.Fatalf("expected one create call, got %d", api.createCalls)
	}

	if err := kube.Create(ctx, ownedAPIKeySecret(cr, "fake-generated-key")); err != nil {
		t.Fatal("create test connection Secret:", err)
	}
	observation, err := e.Observe(ctx, cr)
	if err != nil || !observation.ResourceExists || !observation.ResourceUpToDate {
		t.Fatalf("observe created API key: %+v %v", observation, err)
	}
	if string(observation.ConnectionDetails[apiKeyConnectionSecretKey]) != "fake-generated-key" {
		t.Fatal("observe did not read back the key for connection Secret publication")
	}
	if cr.Status.AtProvider.GroupID != "fake-group" || cr.Status.AtProvider.Label != "test-key" {
		t.Fatal("observation did not record non-secret group-key metadata")
	}

	api.keys[meta.GetExternalName(cr)] = fakeAPIKeyRecord{
		groupID: "fake-group",
		key:     buildbuddy.APIKey{ID: meta.GetExternalName(cr), Label: "drifted", Capabilities: []string{"CACHE_WRITE"}},
		value:   []byte("fake-generated-key"),
	}
	observation, err = e.Observe(ctx, cr)
	if err != nil || observation.ResourceUpToDate {
		t.Fatalf("observe drifted metadata: %+v %v", observation, err)
	}
	updated, err := e.Update(ctx, cr)
	if err != nil {
		t.Fatal("update API-key metadata:", err)
	}
	if api.updateCalls != 1 {
		t.Fatalf("expected one metadata update, got %d", api.updateCalls)
	}
	if string(updated.ConnectionDetails[apiKeyConnectionSecretKey]) != "fake-generated-key" {
		t.Fatal("metadata update did not preserve the existing connection detail")
	}

	cr.DeletionTimestamp = &metav1.Time{}
	if _, err := e.Delete(ctx, cr); err != nil {
		t.Fatal("delete API key:", err)
	}
	if api.deleteCalls != 1 || len(api.keys) != 0 {
		t.Fatal("Delete did not revoke the group-owned key")
	}
}

func TestAPIKeyCreationFailsClosedOnCreateError(t *testing.T) {
	api := &fakeBuildBuddyAPIKey{keys: make(map[string]fakeAPIKeyRecord), createErr: errors.New("fake create failed"), commitBeforeErr: true}
	e := &apiKeyExternal{kube: newAPIKeyTestClient(t), api: api}
	cr := newAPIKeyResource()

	if _, err := e.Create(context.Background(), cr); err == nil || !strings.HasPrefix(meta.GetExternalName(cr), ambiguousExternalName) {
		t.Fatal("ambiguous create did not persist a fail-closed external-name marker")
	}
	if _, err := e.Observe(context.Background(), cr); err == nil {
		t.Fatal("ambiguous create marker did not stop observation and retry")
	}
	if _, err := e.Create(context.Background(), cr); err == nil || api.createCalls != 1 {
		t.Fatal("ambiguous create marker allowed another key creation")
	}
	if _, err := e.Delete(context.Background(), cr); err == nil || api.deleteCalls != 0 {
		t.Fatal("ambiguous create marker allowed unsafe deletion")
	}
	if len(api.keys) != 1 {
		t.Fatal("fake ambiguous Create did not model a possibly committed remote key")
	}
}

func TestAPIKeyReconcilerCreatesAndPreservesConnectionSecret(t *testing.T) {
	ctx := context.Background()
	cr := newAPIKeyResource()
	api := &fakeBuildBuddyAPIKey{keys: make(map[string]fakeAPIKeyRecord)}
	r, kube := newAPIKeyTestReconciler(t, cr, api)
	req := reconcile.Request{NamespacedName: types.NamespacedName{Namespace: cr.Namespace, Name: cr.Name}}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal("reconcile new API key:", err)
	}
	stored := &v1alpha1.APIKey{}
	if err := kube.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatal("get reconciled API key:", err)
	}
	if got := meta.GetExternalName(stored); got != "fake-api-key-id" || got == cr.Name {
		t.Fatalf("expected BuildBuddy-assigned external name, got %q", got)
	}
	secret := &corev1.Secret{}
	secretKey := types.NamespacedName{Namespace: cr.Namespace, Name: cr.Spec.WriteConnectionSecretToReference.Name}
	if err := kube.Get(ctx, secretKey, secret); err != nil {
		t.Fatal("get published API-key Secret:", err)
	}
	controller := metav1.GetControllerOf(secret)
	if controller == nil || controller.UID != cr.UID || controller.Name != cr.Name || controller.Kind != "APIKey" || controller.APIVersion != v1alpha1.SchemeGroupVersion.String() {
		t.Fatalf("publisher set unexpected API-key Secret controller reference: %+v", controller)
	}
	if got := string(secret.Data[apiKeyConnectionSecretKey]); got != "fake-generated-key" {
		t.Fatalf("publisher stored wrong API-key value: %q", got)
	}
	if api.createCalls != 1 {
		t.Fatalf("expected one Create call, got %d", api.createCalls)
	}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal("reconcile existing API key:", err)
	}
	if err := kube.Get(ctx, secretKey, secret); err != nil {
		t.Fatal("get API-key Secret after second reconcile:", err)
	}
	if got := string(secret.Data[apiKeyConnectionSecretKey]); got != "fake-generated-key" {
		t.Fatalf("second observation/publication lost API-key value: %q", got)
	}
	if api.createCalls != 1 {
		t.Fatalf("second reconcile unexpectedly created another key, got %d Create calls", api.createCalls)
	}
	status, err := json.Marshal(stored.Status)
	if err != nil || strings.Contains(string(status), "fake-generated-key") {
		t.Fatal("API-key value was stored in managed-resource status")
	}
}

func TestAPIKeyReconcilerRestoresConnectionSecret(t *testing.T) {
	for _, damage := range []string{"deleted", "empty", "changed"} {
		t.Run(damage, func(t *testing.T) {
			ctx := context.Background()
			cr := newAPIKeyResource()
			api := &fakeBuildBuddyAPIKey{keys: make(map[string]fakeAPIKeyRecord)}
			r, kube := newAPIKeyTestReconciler(t, cr, api)
			req := reconcile.Request{NamespacedName: types.NamespacedName{Namespace: cr.Namespace, Name: cr.Name}}
			if _, err := r.Reconcile(ctx, req); err != nil {
				t.Fatal("create managed API key:", err)
			}
			secret := &corev1.Secret{}
			secretKey := types.NamespacedName{Namespace: cr.Namespace, Name: cr.Spec.WriteConnectionSecretToReference.Name}
			if err := kube.Get(ctx, secretKey, secret); err != nil {
				t.Fatal("get connection Secret:", err)
			}
			if damage == "deleted" {
				if err := kube.Delete(ctx, secret); err != nil {
					t.Fatal("delete connection Secret:", err)
				}
			} else {
				secret.Data = nil
				if damage == "changed" {
					secret.Data = map[string][]byte{apiKeyConnectionSecretKey: []byte("wrong-key")}
				}
				if err := kube.Update(ctx, secret); err != nil {
					t.Fatal("damage connection Secret:", err)
				}
			}
			if _, err := r.Reconcile(ctx, req); err != nil {
				t.Fatal("restore connection Secret:", err)
			}
			if err := kube.Get(ctx, secretKey, secret); err != nil {
				t.Fatal("get restored connection Secret:", err)
			}
			if string(secret.Data[apiKeyConnectionSecretKey]) != "fake-generated-key" || api.createCalls != 1 {
				t.Fatal("reconcile did not restore the original key without creating another")
			}
		})
	}
}

func TestAPIKeyReconcilerRecoversWhenReadbackEnabled(t *testing.T) {
	ctx := context.Background()
	cr := newAPIKeyResource()
	api := &fakeBuildBuddyAPIKey{
		keys:        make(map[string]fakeAPIKeyRecord),
		readbackErr: errors.New("API-key value readback must be enabled"),
	}
	r, kube := newAPIKeyTestReconciler(t, cr, api)
	req := reconcile.Request{NamespacedName: types.NamespacedName{Namespace: cr.Namespace, Name: cr.Name}}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal("reconcile with readback disabled:", err)
	}
	stored := &v1alpha1.APIKey{}
	if err := kube.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatal("get APIKey after readback failure:", err)
	}
	if meta.GetExternalName(stored) != "fake-api-key-id" || !strings.Contains(fmt.Sprint(stored.Status.Conditions), "readback must be enabled") {
		t.Fatal("readback failure must persist the created ID and report the error")
	}
	secret := &corev1.Secret{}
	secretKey := types.NamespacedName{Namespace: cr.Namespace, Name: cr.Spec.WriteConnectionSecretToReference.Name}
	if err := kube.Get(ctx, secretKey, secret); err != nil {
		t.Fatal("get connection Secret after readback failure:", err)
	}
	if len(secret.Data[apiKeyConnectionSecretKey]) != 0 {
		t.Fatal("published a key without successful readback")
	}
	api.readbackErr = nil
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal("reconcile after readback was enabled:", err)
	}
	if err := kube.Get(ctx, secretKey, secret); err != nil {
		t.Fatal("get recovered connection Secret:", err)
	}
	if string(secret.Data[apiKeyConnectionSecretKey]) != "fake-generated-key" || api.createCalls != 1 {
		t.Fatal("did not recover the original key after enabling readback")
	}
}

func TestAPIKeyReconcilerPersistsAmbiguousCreateMarker(t *testing.T) {
	ctx := context.Background()
	cr := newAPIKeyResource()
	api := &fakeBuildBuddyAPIKey{keys: make(map[string]fakeAPIKeyRecord), createErr: errors.New("fake create failed"), commitBeforeErr: true}
	r, kube := newAPIKeyTestReconciler(t, cr, api)
	req := reconcile.Request{NamespacedName: types.NamespacedName{Namespace: cr.Namespace, Name: cr.Name}}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal("reconcile ambiguous API-key create:", err)
	}
	stored := &v1alpha1.APIKey{}
	if err := kube.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatal("get APIKey after ambiguous create:", err)
	}
	if !strings.HasPrefix(meta.GetExternalName(stored), ambiguousExternalName) {
		t.Fatalf("ambiguous marker was not persisted: %q", meta.GetExternalName(stored))
	}
	if stored.Annotations[meta.AnnotationKeyExternalCreateFailed] == "" {
		t.Fatal("Crossplane did not persist external-create-failed after Create returned an error")
	}
	if len(api.keys) != 1 {
		t.Fatal("fake ambiguous Create did not model a possibly committed remote key")
	}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal("reconcile persisted ambiguous marker:", err)
	}
	if api.createCalls != 1 {
		t.Fatalf("reconciler retried an ambiguous Create %d times", api.createCalls)
	}

	// An operator can identify the key and replace the marker with its ID.
	if err := kube.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatal("get APIKey before resolving its ID:", err)
	}
	meta.SetExternalName(stored, "fake-api-key-id")
	if err := kube.Update(ctx, stored); err != nil {
		t.Fatal("resolve ambiguous key ID:", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal("reconcile identified key:", err)
	}
	secret := &corev1.Secret{}
	secretKey := types.NamespacedName{Namespace: cr.Namespace, Name: cr.Spec.WriteConnectionSecretToReference.Name}
	if err := kube.Get(ctx, secretKey, secret); err != nil {
		t.Fatal("get recovered connection Secret:", err)
	}
	if string(secret.Data[apiKeyConnectionSecretKey]) != "fake-generated-key" || api.createCalls != 1 {
		t.Fatal("identified key was not recovered without another create")
	}
}

func TestAPIKeyReconcilerDoesNotAdoptUnrelatedOutputSecret(t *testing.T) {
	ctx := context.Background()
	cr := newAPIKeyResource()
	unrelated := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: cr.Spec.WriteConnectionSecretToReference.Name, Namespace: cr.Namespace},
		Data:       map[string][]byte{apiKeyConnectionSecretKey: []byte("unrelated-value")},
	}
	api := &fakeBuildBuddyAPIKey{keys: make(map[string]fakeAPIKeyRecord)}
	r, kube := newAPIKeyTestReconciler(t, cr, api, unrelated)
	req := reconcile.Request{NamespacedName: types.NamespacedName{Namespace: cr.Namespace, Name: cr.Name}}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal("reconcile with unrelated output Secret:", err)
	}
	secret := &corev1.Secret{}
	secretKey := types.NamespacedName{Namespace: cr.Namespace, Name: cr.Spec.WriteConnectionSecretToReference.Name}
	if err := kube.Get(ctx, secretKey, secret); err != nil {
		t.Fatal("get unrelated Secret after reconcile:", err)
	}
	if string(secret.Data[apiKeyConnectionSecretKey]) != "unrelated-value" || len(secret.OwnerReferences) != 0 {
		t.Fatal("reconciler overwrote or adopted an unrelated output Secret")
	}
	if api.createCalls != 0 {
		t.Fatal("reconciler created a remote key when the output Secret could not be safely owned")
	}
}

func TestAPIKeyReconcilerDoesNotOverwriteUnrelatedSecretOnReadback(t *testing.T) {
	ctx := context.Background()
	cr := newAPIKeyResource()
	meta.SetExternalName(cr, "existing-id")
	unrelated := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: cr.Spec.WriteConnectionSecretToReference.Name, Namespace: cr.Namespace},
		Data:       map[string][]byte{apiKeyConnectionSecretKey: []byte("unrelated-value")},
	}
	api := &fakeBuildBuddyAPIKey{keys: map[string]fakeAPIKeyRecord{
		"existing-id": {groupID: "fake-group", key: buildbuddy.APIKey{ID: "existing-id"}, value: []byte("remote-value")},
	}}
	r, kube := newAPIKeyTestReconciler(t, cr, api, unrelated)
	req := reconcile.Request{NamespacedName: types.NamespacedName{Namespace: cr.Namespace, Name: cr.Name}}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal("reconcile existing key with unrelated output Secret:", err)
	}
	secret := &corev1.Secret{}
	if err := kube.Get(ctx, types.NamespacedName{Namespace: cr.Namespace, Name: unrelated.Name}, secret); err != nil {
		t.Fatal("get unrelated Secret:", err)
	}
	if string(secret.Data[apiKeyConnectionSecretKey]) != "unrelated-value" || len(secret.OwnerReferences) != 0 {
		t.Fatal("reconcile overwrote or adopted an unrelated output Secret")
	}
}

func TestAPIKeyEmptyCapabilitiesCanBeCreatedAndCleared(t *testing.T) {
	ctx := context.Background()
	createCR := newAPIKeyResource()
	createCR.Spec.ForProvider.Capabilities = nil
	api := &fakeBuildBuddyAPIKey{keys: make(map[string]fakeAPIKeyRecord)}
	e := &apiKeyExternal{kube: newAPIKeyTestClient(t), api: api}
	if _, err := e.Create(ctx, createCR); err != nil {
		t.Fatal("create read-only API key:", err)
	}
	if got := api.keys[meta.GetExternalName(createCR)].key.Capabilities; len(got) != 0 {
		t.Fatalf("created API key has unexpected capabilities: %v", got)
	}

	updateCR := newAPIKeyResource()
	meta.SetExternalName(updateCR, "existing-api-key-id")
	if err := e.kube.Create(ctx, ownedAPIKeySecret(updateCR, "fake-existing-key")); err != nil {
		t.Fatal("create existing API-key connection Secret:", err)
	}
	api.keys[meta.GetExternalName(updateCR)] = fakeAPIKeyRecord{
		groupID: "fake-group",
		key:     buildbuddy.APIKey{ID: meta.GetExternalName(updateCR), Capabilities: []string{"CACHE_WRITE"}},
		value:   []byte("fake-existing-key"),
	}
	updateCR.Spec.ForProvider.Capabilities = nil
	if _, err := e.Update(ctx, updateCR); err != nil {
		t.Fatal("clear API-key capabilities:", err)
	}
	if got := api.keys[meta.GetExternalName(updateCR)].key.Capabilities; len(got) != 0 {
		t.Fatalf("updated API key still has capabilities: %v", got)
	}
}

func TestAPIKeyGroupScopeAndConnectionSecretRestrictions(t *testing.T) {
	api := &fakeBuildBuddyAPIKey{keys: map[string]fakeAPIKeyRecord{
		"foreign-id": {groupID: "another-group", key: buildbuddy.APIKey{ID: "foreign-id", Label: "foreign"}},
	}}
	e := &apiKeyExternal{kube: newAPIKeyTestClient(t), api: api}
	cr := newAPIKeyResource()
	meta.SetExternalName(cr, "foreign-id")
	cr.DeletionTimestamp = &metav1.Time{}
	if _, err := e.Delete(context.Background(), cr); err != nil {
		t.Fatal("delete foreign group key:", err)
	}
	if api.deleteCalls != 0 {
		t.Fatal("provider attempted to delete a key outside the configured group")
	}

	for _, name := range []string{"input", "credentials"} {
		candidate := newAPIKeyResource()
		candidate.Spec.WriteConnectionSecretToReference.Name = name
		if err := validateAPIKeyConnectionSecret(candidate, "input", "credentials"); err == nil {
			t.Fatalf("accepted output Secret name %q", name)
		}
	}
	missingRef := newAPIKeyResource()
	missingRef.Spec.WriteConnectionSecretToReference = nil
	if err := validateAPIKeyConnectionSecret(missingRef, "input", "credentials"); err == nil {
		t.Fatal("accepted a key without a writeConnectionSecretToRef")
	}
}

func TestAPIKeyCreateRequiresUnusedOutputSecret(t *testing.T) {
	ctx := context.Background()
	cr := newAPIKeyResource()
	kube := newAPIKeyTestClient(t)
	if err := kube.Create(ctx, ownedAPIKeySecret(cr, "existing-value")); err != nil {
		t.Fatal("create pre-existing output Secret:", err)
	}
	api := &fakeBuildBuddyAPIKey{keys: make(map[string]fakeAPIKeyRecord)}
	e := &apiKeyExternal{kube: kube, api: api}
	if _, err := e.Create(ctx, cr); err == nil || api.createCalls != 0 {
		t.Fatal("created a remote key when its connection Secret could not be safely published")
	}
}

func newAPIKeyResource() *v1alpha1.APIKey {
	return &v1alpha1.APIKey{
		ObjectMeta: metav1.ObjectMeta{Name: "test-key", Namespace: "test", UID: types.UID("fake-api-key-uid")},
		Spec: v1alpha1.APIKeySpec{
			ManagedResourceSpec: xpv2.ManagedResourceSpec{
				ManagementPolicies:               xpv2.ManagementPolicies{xpv2.ManagementActionAll},
				WriteConnectionSecretToReference: &xpv2.LocalSecretReference{Name: "api-key-output"},
			},
			ForProvider: v1alpha1.APIKeyParameters{
				GroupID:             "fake-group",
				Label:               "test-key",
				Capabilities:        []v1alpha1.APIKeyCapability{v1alpha1.APIKeyCapabilityCacheWrite},
				VisibleToDevelopers: true,
			},
		},
	}
}

func newAPIKeyTestClient(t *testing.T, objects ...client.Object) client.Client {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	return fake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()
}

func newAPIKeyTestReconciler(t *testing.T, cr *v1alpha1.APIKey, api *fakeBuildBuddyAPIKey, objects ...client.Object) (*managed.Reconciler, client.Client) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := v1alpha1.SchemeBuilder.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	allObjects := append([]client.Object{cr}, objects...)
	kube := fake.NewClientBuilder().WithScheme(scheme).WithObjects(allObjects...).WithStatusSubresource(&v1alpha1.APIKey{}).Build()
	mgr := &apiKeyTestManager{kube: kube, scheme: scheme}
	r := newAPIKeyReconciler(mgr, &apiKeyTestConnector{external: &apiKeyExternal{kube: kube, api: api}})
	return r, kube
}

func ownedAPIKeySecret(cr *v1alpha1.APIKey, value string) *corev1.Secret {
	controller := true
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      cr.Spec.WriteConnectionSecretToReference.Name,
			Namespace: cr.Namespace,
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: v1alpha1.SchemeGroupVersion.String(),
				Kind:       "APIKey",
				Name:       cr.Name,
				UID:        cr.UID,
				Controller: &controller,
			}},
		},
		Data: map[string][]byte{apiKeyConnectionSecretKey: []byte(value)},
	}
}
