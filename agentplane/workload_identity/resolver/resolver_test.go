package resolver_test

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"os"
	"testing"
	"time"

	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
)

const (
	servedNamespace   = "test-workloads"
	unservedNamespace = "test-elsewhere"
	policyNamespace   = "test-policies"
	subjectAccount    = "test-agent"
	subjectIdentity   = "test-agent-identity"
	tokenAudience     = "test-kubernetes"
)

var (
	api     *kubeapiserver.Server
	objects client.WithWatch
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) error {
		api = server
		scheme := runtime.NewScheme()
		for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, workloadapi.AddToScheme, policyapi.AddToScheme} {
			if err := add(scheme); err != nil {
				return err
			}
		}
		var err error
		if objects, err = client.NewWithWatch(server.Config, client.Options{Scheme: scheme}); err != nil {
			return err
		}
		ctx := context.Background()
		for _, namespace := range []string{servedNamespace, unservedNamespace} {
			if _, err := server.ServiceAccount(ctx, auth.Account{Namespace: namespace, Name: subjectAccount}); err != nil {
				return err
			}
		}
		if err := objects.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: policyNamespace}}); err != nil {
			return err
		}
		// The same name in the record's own namespace is not the subject: Identities live in the
		// policy namespace.
		for _, namespace := range []string{policyNamespace, servedNamespace} {
			identity := &policyapi.Identity{
				ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: subjectIdentity},
				Spec:       policyapi.IdentitySpec{Description: "test agent"},
			}
			if err := objects.Create(ctx, identity); err != nil {
				return err
			}
		}
		return nil
	}))
}

// The Sandbox a record names as its owner is data: no Sandbox exists here, and the resolver never
// looks for one.
func sandboxOwner(name string) *metav1.OwnerReference {
	return &metav1.OwnerReference{
		APIVersion: "agentplane.allegedly.works/v1alpha1", Kind: "Sandbox", Name: name,
		UID: types.UID("0f0e0d0c-0b0a-4908-8706-050403020100"), Controller: new(true),
	}
}

var credentialIDs = []string{"0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210"}

type issued struct {
	record *workloadapi.WorkloadCredential
	leaf   *x509.Certificate
}

// issue writes a record and the status the issuer would, and a leaf for the status's URI.
func issue(t *testing.T, ca *transporttesting.CA, namespace, name string, owner *metav1.OwnerReference, spec workloadapi.WorkloadCredentialSpec, accepted *workloadapi.Binding, id string) issued {
	t.Helper()
	ctx := t.Context()
	record := &workloadapi.WorkloadCredential{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name}, Spec: spec}
	if owner != nil {
		record.OwnerReferences = []metav1.OwnerReference{*owner}
	}
	if err := objects.Create(ctx, record); err != nil {
		t.Fatal(err)
	}
	workload := transport.WorkloadID{Namespace: namespace, Name: name, CredentialID: id}
	uri, err := workload.URI()
	if err != nil {
		t.Fatal(err)
	}
	record.Status = workloadapi.WorkloadCredentialStatus{
		ObservedGeneration: record.Generation, CredentialID: id, URI: uri, SecretName: "svid-" + name + "-" + id,
		AcceptedBinding: accepted,
	}
	if err := objects.Status().Update(ctx, record); err != nil {
		t.Fatal(err)
	}
	return issued{record: record, leaf: leafFor(t, ca, uri, x509.ExtKeyUsageClientAuth, x509.ExtKeyUsageServerAuth)}
}

func leafFor(t *testing.T, ca *transporttesting.CA, uri string, usages ...x509.ExtKeyUsage) *x509.Certificate {
	t.Helper()
	config := ca.IssueWithUsage(t, uri, usages...)
	pair, err := tls.LoadX509KeyPair(config.CertFile, config.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	return leaf
}

func newResolver(t *testing.T, reader client.Reader) *resolver.Resolver {
	t.Helper()
	resolves, err := resolver.New(reader, []string{servedNamespace}, &resolver.Identities{PolicyNamespace: policyNamespace})
	if err != nil {
		t.Fatal(err)
	}
	return resolves
}

func podSpec(uid types.UID) workloadapi.WorkloadCredentialSpec {
	return workloadapi.WorkloadCredentialSpec{
		Subject: workloadapi.Subject{ServiceAccount: subjectAccount}, Binding: &workloadapi.Binding{PodUID: uid},
		Pod: &workloadapi.PodRef{Name: "test-pod", UID: uid},
	}
}

func identityPodSpec(pod, identity types.UID) workloadapi.WorkloadCredentialSpec {
	spec := podSpec(pod)
	spec.Subject = workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: subjectIdentity, UID: identity}}
	return spec
}

func identityUID(t *testing.T, namespace string) types.UID {
	t.Helper()
	var identity policyapi.Identity
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: namespace, Name: subjectIdentity}, &identity); err != nil {
		t.Fatal(err)
	}
	return identity.UID
}

func subjectUID(t *testing.T, namespace string) types.UID {
	t.Helper()
	var account corev1.ServiceAccount
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: namespace, Name: subjectAccount}, &account); err != nil {
		t.Fatal(err)
	}
	return account.UID
}

func requireRefused(t *testing.T, resolves *resolver.Resolver, leaf *x509.Certificate) {
	t.Helper()
	if peer, err := resolves.Resolve(t.Context(), leaf); !errors.Is(err, resolver.ErrRefused) {
		t.Fatalf("resolved %+v, %v; want refused", peer, err)
	}
}

func TestAnAcceptedIncarnationResolvesToItsSubject(t *testing.T) {
	ca := transporttesting.NewCA(t)
	pod := types.UID("11111111-1111-4111-8111-111111111111")
	owned := issue(t, ca, servedNamespace, "test-accepted", sandboxOwner("test-accepted"), podSpec(pod), &workloadapi.Binding{PodUID: pod}, credentialIDs[0])
	peer, err := newResolver(t, objects).Resolve(t.Context(), owned.leaf)
	if err != nil {
		t.Fatal(err)
	}
	var want resolver.Subject = resolver.ServiceAccount{Namespace: servedNamespace, Name: subjectAccount, UID: subjectUID(t, servedNamespace)}
	if peer.Subject != want || peer.Binding == nil || peer.Binding.PodUID != pod || peer.Owner == nil || peer.Owner.Kind != "Sandbox" ||
		peer.Workload != (transport.WorkloadID{Namespace: servedNamespace, Name: "test-accepted", CredentialID: credentialIDs[0]}) {
		t.Fatalf("resolved %+v", peer)
	}
}

func TestAnIdentitySubjectResolvesToTheLiveIdentity(t *testing.T) {
	ca := transporttesting.NewCA(t)
	pod := types.UID("12121212-1212-4121-8121-121212121212")
	uid := identityUID(t, policyNamespace)
	record := issue(t, ca, servedNamespace, "test-identity-accepted", sandboxOwner("test-identity-accepted"), identityPodSpec(pod, uid), &workloadapi.Binding{PodUID: pod}, credentialIDs[0])
	peer, err := newResolver(t, objects).Resolve(t.Context(), record.leaf)
	if err != nil {
		t.Fatal(err)
	}
	if peer.Subject != (resolver.Identity{Name: subjectIdentity, UID: uid}) || peer.Pod == nil || peer.Pod.UID != pod {
		t.Fatalf("resolved %+v", peer)
	}
}

// Deleting an Identity revokes every credential naming it, and a same-named successor is another
// incarnation.
func TestADeletedIdentityRevokesItsCredentials(t *testing.T) {
	ctx := t.Context()
	ca := transporttesting.NewCA(t)
	pod := types.UID("13131313-1313-4131-8131-131313131313")
	identity := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: "test-deleted-identity"},
		Spec:       policyapi.IdentitySpec{Description: "test agent"},
	}
	if err := objects.Create(ctx, identity); err != nil {
		t.Fatal(err)
	}
	spec := identityPodSpec(pod, identity.UID)
	spec.Subject.Identity.Name = identity.Name
	record := issue(t, ca, servedNamespace, "test-deleted-identity", sandboxOwner("test-deleted-identity"), spec, &workloadapi.Binding{PodUID: pod}, credentialIDs[0])
	resolves := newResolver(t, objects)
	if _, err := resolves.Resolve(ctx, record.leaf); err != nil {
		t.Fatal(err)
	}
	if err := objects.Delete(ctx, identity); err != nil {
		t.Fatal(err)
	}
	requireRefused(t, resolves, record.leaf)
	successor := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: identity.Name}, Spec: identity.Spec}
	if err := objects.Create(ctx, successor); err != nil {
		t.Fatal(err)
	}
	requireRefused(t, resolves, record.leaf)
}

// A standing workload has no owner and no binding; its record alone is its incarnation.
func TestAStandingWorkloadNeedsNoBinding(t *testing.T) {
	ca := transporttesting.NewCA(t)
	standing := issue(t, ca, servedNamespace, "test-standing", nil, workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{ServiceAccount: subjectAccount}}, nil, credentialIDs[0])
	peer, err := newResolver(t, objects).Resolve(t.Context(), standing.leaf)
	if subject, ok := peer.Subject.(resolver.ServiceAccount); err != nil || peer.Binding != nil || peer.Owner != nil || !ok || subject.UID != subjectUID(t, servedNamespace) {
		t.Fatalf("resolved %+v, %v", peer, err)
	}
}

func TestRefusals(t *testing.T) {
	ca := transporttesting.NewCA(t)
	pod := types.UID("22222222-2222-4222-8222-222222222222")
	other := types.UID("33333333-3333-4333-8333-333333333333")
	accepted := &workloadapi.Binding{PodUID: pod}
	resolves := newResolver(t, objects)
	sameURI := func(record issued, usages ...x509.ExtKeyUsage) *x509.Certificate {
		return leafFor(t, ca, record.record.Status.URI, usages...)
	}

	for name, leaf := range map[string]func(t *testing.T) *x509.Certificate{
		"the Sandbox URI form": func(t *testing.T) *x509.Certificate {
			return leafFor(t, ca, "spiffe://agentplane/environment-runtime/"+servedNamespace+"/"+string(pod)+"/"+credentialIDs[0], x509.ExtKeyUsageClientAuth)
		},
		"a server-only certificate": func(t *testing.T) *x509.Certificate {
			return sameURI(issue(t, ca, servedNamespace, "test-server-only", sandboxOwner("test-server-only"), podSpec(pod), accepted, credentialIDs[0]), x509.ExtKeyUsageServerAuth)
		},
		"an expired certificate": func(t *testing.T) *x509.Certificate {
			record := issue(t, ca, servedNamespace, "test-expired", sandboxOwner("test-expired"), podSpec(pod), accepted, credentialIDs[0])
			config := ca.IssueWithValidity(t, record.record.Status.URI, time.Now().Add(-2*time.Hour), time.Now().Add(-time.Hour), x509.ExtKeyUsageClientAuth)
			pair, err := tls.LoadX509KeyPair(config.CertFile, config.KeyFile)
			if err != nil {
				t.Fatal(err)
			}
			leaf, err := x509.ParseCertificate(pair.Certificate[0])
			if err != nil {
				t.Fatal(err)
			}
			return leaf
		},
		"a namespace the verifier does not serve": func(t *testing.T) *x509.Certificate {
			return issue(t, ca, unservedNamespace, "test-unserved", sandboxOwner("test-unserved"), podSpec(pod), accepted, credentialIDs[0]).leaf
		},
		"no record": func(t *testing.T) *x509.Certificate {
			uri, err := transport.WorkloadID{Namespace: servedNamespace, Name: "test-missing", CredentialID: credentialIDs[0]}.URI()
			if err != nil {
				t.Fatal(err)
			}
			return leafFor(t, ca, uri, x509.ExtKeyUsageClientAuth)
		},
		"a rotated credential ID": func(t *testing.T) *x509.Certificate {
			record := issue(t, ca, servedNamespace, "test-rotated", sandboxOwner("test-rotated"), podSpec(pod), accepted, credentialIDs[1])
			uri, err := transport.WorkloadID{Namespace: servedNamespace, Name: record.record.Name, CredentialID: credentialIDs[0]}.URI()
			if err != nil {
				t.Fatal(err)
			}
			return leafFor(t, ca, uri, x509.ExtKeyUsageClientAuth)
		},
		"an owned record with no accepted binding": func(t *testing.T) *x509.Certificate {
			return issue(t, ca, servedNamespace, "test-unaccepted", sandboxOwner("test-unaccepted"), podSpec(pod), nil, credentialIDs[0]).leaf
		},
		"an owned record before its incarnation exists": func(t *testing.T) *x509.Certificate {
			spec := workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{ServiceAccount: subjectAccount}}
			return issue(t, ca, servedNamespace, "test-unbound", sandboxOwner("test-unbound"), spec, nil, credentialIDs[0]).leaf
		},
		"a binding the owner cleared": func(t *testing.T) *x509.Certificate {
			spec := workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{ServiceAccount: subjectAccount}}
			return issue(t, ca, servedNamespace, "test-cleared", sandboxOwner("test-cleared"), spec, accepted, credentialIDs[0]).leaf
		},
		"a binding the owner replaced": func(t *testing.T) *x509.Certificate {
			return issue(t, ca, servedNamespace, "test-replaced", sandboxOwner("test-replaced"), podSpec(other), accepted, credentialIDs[0]).leaf
		},
		"an ownerless record with a binding but no acceptance": func(t *testing.T) *x509.Certificate {
			return issue(t, ca, servedNamespace, "test-ownerless", nil, podSpec(pod), nil, credentialIDs[0]).leaf
		},
		"a subject ServiceAccount that does not exist": func(t *testing.T) *x509.Certificate {
			spec := podSpec(pod)
			spec.Subject.ServiceAccount = "test-missing-account"
			return issue(t, ca, servedNamespace, "test-no-account", sandboxOwner("test-no-account"), spec, accepted, credentialIDs[0]).leaf
		},
		"an Identity subject with another UID": func(t *testing.T) *x509.Certificate {
			return issue(t, ca, servedNamespace, "test-identity-uid", sandboxOwner("test-identity-uid"), identityPodSpec(pod, other), accepted, credentialIDs[0]).leaf
		},
		"an Identity subject that exists only in the record's namespace": func(t *testing.T) *x509.Certificate {
			spec := identityPodSpec(pod, identityUID(t, servedNamespace))
			return issue(t, ca, servedNamespace, "test-identity-local", sandboxOwner("test-identity-local"), spec, accepted, credentialIDs[0]).leaf
		},
		"an Identity subject that does not exist": func(t *testing.T) *x509.Certificate {
			spec := identityPodSpec(pod, other)
			spec.Subject.Identity.Name = "test-missing-identity"
			return issue(t, ca, servedNamespace, "test-identity-missing", sandboxOwner("test-identity-missing"), spec, accepted, credentialIDs[0]).leaf
		},
	} {
		t.Run(name, func(t *testing.T) { requireRefused(t, resolves, leaf(t)) })
	}
}

// A name a later owner reuses gets a new record and a fresh ID; the earlier incarnation's
// certificate names the same record but not its ID.
func TestAStaleCredentialIsRefusedAfterNameReuse(t *testing.T) {
	ca := transporttesting.NewCA(t)
	pod := types.UID("44444444-4444-4444-8444-444444444444")
	accepted := &workloadapi.Binding{PodUID: pod}
	first := issue(t, ca, servedNamespace, "test-reused", sandboxOwner("test-reused"), podSpec(pod), accepted, credentialIDs[0])
	resolves := newResolver(t, objects)
	if _, err := resolves.Resolve(t.Context(), first.leaf); err != nil {
		t.Fatal(err)
	}
	if err := objects.Delete(t.Context(), first.record); err != nil {
		t.Fatal(err)
	}
	requireRefused(t, resolves, first.leaf)
	second := issue(t, ca, servedNamespace, "test-reused", sandboxOwner("test-reused"), podSpec(pod), accepted, credentialIDs[1])
	requireRefused(t, resolves, first.leaf)
	if _, err := resolves.Resolve(t.Context(), second.leaf); err != nil {
		t.Fatal(err)
	}
}

func TestADeletingRecordIsRefused(t *testing.T) {
	ca := transporttesting.NewCA(t)
	pod := types.UID("55555555-5555-4555-8555-555555555555")
	deleting := issue(t, ca, servedNamespace, "test-deleting", sandboxOwner("test-deleting"), podSpec(pod), &workloadapi.Binding{PodUID: pod}, credentialIDs[0])
	deleting.record.Finalizers = []string{"test.agentplane.allegedly.works/hold"}
	if err := objects.Update(t.Context(), deleting.record); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		deleting.record.Finalizers = nil
		if err := objects.Update(context.Background(), deleting.record); err != nil {
			t.Error(err)
		}
	})
	if err := objects.Delete(t.Context(), deleting.record); err != nil {
		t.Fatal(err)
	}
	if err := objects.Get(t.Context(), client.ObjectKeyFromObject(deleting.record), deleting.record); err != nil || deleting.record.DeletionTimestamp.IsZero() {
		t.Fatalf("record not held in deletion: %v", err)
	}
	requireRefused(t, newResolver(t, objects), deleting.leaf)
}

// Step 7: a Pod-bound token the caller forwards must come from the accepted Pod. The Pod UID is
// what the API server's TokenReview reports for it.
func TestAForwardedPodTokenMustComeFromTheAcceptedPod(t *testing.T) {
	ctx := t.Context()
	ca := transporttesting.NewCA(t)
	reviewed := func(pod *corev1.Pod) types.UID {
		t.Helper()
		token, err := api.PodToken(ctx, pod, tokenAudience)
		if err != nil {
			t.Fatal(err)
		}
		review, err := api.TokenReviews().Create(ctx, &authenticationv1.TokenReview{Spec: authenticationv1.TokenReviewSpec{Token: token, Audiences: []string{tokenAudience}}}, metav1.CreateOptions{})
		if err != nil || !review.Status.Authenticated {
			t.Fatalf("review: %+v, %v", review, err)
		}
		return types.UID(review.Status.User.Extra["authentication.kubernetes.io/pod-uid"][0])
	}
	account := auth.Account{Namespace: servedNamespace, Name: subjectAccount}
	acceptedPod, err := api.Pod(ctx, account)
	if err != nil {
		t.Fatal(err)
	}
	otherPod, err := api.Pod(ctx, account)
	if err != nil {
		t.Fatal(err)
	}
	accepted := &workloadapi.Binding{PodUID: acceptedPod.UID}
	record := issue(t, ca, servedNamespace, "test-pod-token", sandboxOwner("test-pod-token"), podSpec(acceptedPod.UID), accepted, credentialIDs[0])
	resolves := newResolver(t, objects)
	peer, err := resolves.Resolve(ctx, record.leaf)
	if err != nil {
		t.Fatal(err)
	}
	if err := peer.RequirePod(reviewed(acceptedPod)); err != nil {
		t.Fatal(err)
	}
	if err := peer.RequirePod(reviewed(otherPod)); !errors.Is(err, resolver.ErrRefused) {
		t.Fatalf("a token from another Pod passed: %v", err)
	}

	// A VMI's tokens come from its virt-launcher Pod, once its owner has named it.
	vmi := &workloadapi.Binding{VMIUID: "77777777-7777-4777-8777-777777777777"}
	spec := workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{ServiceAccount: subjectAccount}, Binding: vmi}
	unnamed := issue(t, ca, servedNamespace, "test-vmi-unnamed", sandboxOwner("test-vmi-unnamed"), spec, vmi, credentialIDs[0])
	if peer, err = resolves.Resolve(ctx, unnamed.leaf); err != nil || peer.Pod != nil {
		t.Fatalf("resolved %+v, %v", peer, err)
	}
	if err := peer.RequirePod(reviewed(acceptedPod)); !errors.Is(err, resolver.ErrRefused) {
		t.Fatalf("a VMI with no launcher named accepted a Pod token: %v", err)
	}
	spec.Pod = &workloadapi.PodRef{Name: acceptedPod.Name, UID: acceptedPod.UID}
	launched := issue(t, ca, servedNamespace, "test-vmi-launched", sandboxOwner("test-vmi-launched"), spec, vmi, credentialIDs[0])
	if peer, err = resolves.Resolve(ctx, launched.leaf); err != nil || *peer.Pod != *spec.Pod {
		t.Fatalf("resolved %+v, %v", peer, err)
	}
	if err := peer.RequirePod(reviewed(acceptedPod)); err != nil {
		t.Fatal(err)
	}
	if err := peer.RequirePod(reviewed(otherPod)); !errors.Is(err, resolver.ErrRefused) {
		t.Fatalf("a token from another Pod passed for a VMI: %v", err)
	}
}

func TestAnAPIErrorIsUnavailableNotRefused(t *testing.T) {
	ca := transporttesting.NewCA(t)
	pod := types.UID("66666666-6666-4666-8666-666666666666")
	record := issue(t, ca, servedNamespace, "test-unavailable", sandboxOwner("test-unavailable"), podSpec(pod), &workloadapi.Binding{PodUID: pod}, credentialIDs[0])
	failing := interceptor.NewClient(objects, interceptor.Funcs{
		Get: func(context.Context, client.WithWatch, client.ObjectKey, client.Object, ...client.GetOption) error {
			return errors.New("test API server outage")
		},
	})
	if _, err := newResolver(t, failing).Resolve(t.Context(), record.leaf); !errors.Is(err, resolver.ErrUnavailable) {
		t.Fatalf("an API error resolved as %v", err)
	}

	identityRecord := issue(t, ca, servedNamespace, "test-identity-unavailable", sandboxOwner("test-identity-unavailable"), identityPodSpec(pod, identityUID(t, policyNamespace)), &workloadapi.Binding{PodUID: pod}, credentialIDs[0])
	identitiesFailing := interceptor.NewClient(objects, interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, object client.Object, options ...client.GetOption) error {
			if _, ok := object.(*policyapi.Identity); ok {
				return errors.New("test API server outage")
			}
			return c.Get(ctx, key, object, options...)
		},
	})
	if _, err := newResolver(t, identitiesFailing).Resolve(t.Context(), identityRecord.leaf); !errors.Is(err, resolver.ErrUnavailable) {
		t.Fatalf("an API error reading the Identity resolved as %v", err)
	}
}

// A verifier not configured with the policy namespace resolves no Identity subject.
func TestAVerifierWithoutThePolicyNamespaceRefusesIdentitySubjects(t *testing.T) {
	ca := transporttesting.NewCA(t)
	pod := types.UID("14141414-1414-4141-8141-141414141414")
	record := issue(t, ca, servedNamespace, "test-identity-unconfigured", sandboxOwner("test-identity-unconfigured"), identityPodSpec(pod, identityUID(t, policyNamespace)), &workloadapi.Binding{PodUID: pod}, credentialIDs[0])
	resolves, err := resolver.New(objects, []string{servedNamespace}, nil)
	if err != nil {
		t.Fatal(err)
	}
	requireRefused(t, resolves, record.leaf)
}
