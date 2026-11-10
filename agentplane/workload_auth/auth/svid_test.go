package auth_test

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net/url"
	"testing"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	authlistener "github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"github.com/google/uuid"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestSVIDResolvesTheRecordsSubjectAndPod(t *testing.T) {
	for _, binding := range []string{"Pod", "VMI"} {
		t.Run(binding, func(t *testing.T) {
			workload, leaf := svidWorkload(t)
			want := workload.Principal
			if binding == "VMI" {
				// A VMI's caller Pod is its virt-launcher Pod, which the owner names in spec.pod.
				record := workload.Record
				record.Spec.Binding = &workloadapi.Binding{VMIUID: types.UID(uuid.NewString())}
				record.Spec.Pod = &workloadapi.PodRef{Name: "test-launcher", UID: types.UID(uuid.NewString())}
				if err := workload.Objects.Update(t.Context(), record); err != nil {
					t.Fatal(err)
				}
				leaf.URIs[0] = mustURL(t, workload.Issue(t, "cccccccccccccccccccccccccccccccc"))
				want.PodName, want.PodUID = record.Spec.Pod.Name, string(record.Spec.Pod.UID)
			}
			resolver, err := auth.NewSVIDResolver(workload.Objects, []string{workload.Sandbox.Namespace}, nil)
			if err != nil {
				t.Fatal(err)
			}
			got, err := resolver.ResolveVerifiedLeaf(t.Context(), leaf)
			if err != nil {
				t.Fatal(err)
			}
			if got != want {
				t.Fatalf("principal = %#v, want %#v", got, want)
			}
		})
	}
}

// A standing workload's record has no owner and no binding: it proves its subject and no Pod, and a
// reviewed-caller method admits it without one.
func TestSVIDResolvesAStandingWorkloadWithoutAPod(t *testing.T) {
	workload, leaf := svidWorkload(t)
	record := workload.Record
	record.OwnerReferences, record.Spec.Binding, record.Spec.Pod = nil, nil, nil
	if err := workload.Objects.Update(t.Context(), record); err != nil {
		t.Fatal(err)
	}
	leaf.URIs[0] = mustURL(t, workload.Issue(t, "dddddddddddddddddddddddddddddddd"))
	svids, err := auth.NewSVIDResolver(workload.Objects, []string{workload.Sandbox.Namespace}, nil)
	if err != nil {
		t.Fatal(err)
	}
	want := workload.Principal
	want.PodName, want.PodUID = "", ""
	if got, err := svids.ResolveVerifiedLeaf(t.Context(), leaf); err != nil || got != want {
		t.Fatalf("principal = %#v, %v; want %#v", got, err, want)
	}

	resolver, err := newResolver(t, api.TokenReviews()).WithListenerAuthentication(authlistener.Config{
		ListenAddress: "test",
		TLS:           &authlistener.TLS{CertFile: "/test/tls.crt", KeyFile: "/test/tls.key"},
		Auth:          []authlistener.Authentication{&authlistener.SPIFFEAuth{CAFile: "/test/ca.pem"}},
	}, svids)
	if err != nil {
		t.Fatal(err)
	}
	ctx := peer.NewContext(t.Context(), &peer.Peer{AuthInfo: credentials.TLSInfo{
		State: tls.ConnectionState{PeerCertificates: []*x509.Certificate{leaf}},
	}})
	caller, err := resolver.ResolveReviewedCaller(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if caller.Pod != nil || caller.ServiceAccount.GetUid() != want.ServiceAccountUID ||
		caller.ServiceAccount.GetAccount().GetName() != want.Account.Name {
		t.Fatalf("reviewed caller = %#v, want the subject and no Pod", caller)
	}
}

func TestSVIDRefusesRevokedOrIncompleteWorkloads(t *testing.T) {
	for _, test := range []struct {
		name   string
		change func(*testing.T, workloadauthtesting.Workload, *x509.Certificate)
	}{
		{"another record", func(_ *testing.T, w workloadauthtesting.Workload, c *x509.Certificate) {
			c.URIs[0].Path = "/" + w.Record.Namespace + "/replaced/" + w.Record.Status.CredentialID
		}},
		{"rotated credential", func(t *testing.T, w workloadauthtesting.Workload, _ *x509.Certificate) {
			w.Issue(t, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
		}},
		{"Sandbox URI form", func(_ *testing.T, w workloadauthtesting.Workload, c *x509.Certificate) {
			c.URIs[0].Path = "/environment-runtime/" + w.Record.Namespace + "/" + string(w.Sandbox.UID) + "/" + w.Record.Status.CredentialID
		}},
		{"cleared binding", func(t *testing.T, w workloadauthtesting.Workload, _ *x509.Certificate) {
			w.Record.Spec.Binding, w.Record.Spec.Pod = nil, nil
			if err := w.Objects.Update(t.Context(), w.Record); err != nil {
				t.Fatal(err)
			}
		}},
		{"revoked record", func(t *testing.T, w workloadauthtesting.Workload, _ *x509.Certificate) { w.Revoke(t) }},
		{"expired leaf", func(_ *testing.T, _ workloadauthtesting.Workload, c *x509.Certificate) {
			c.NotAfter = time.Now().Add(-time.Minute)
		}},
		{"not yet valid leaf", func(_ *testing.T, _ workloadauthtesting.Workload, c *x509.Certificate) {
			c.NotBefore = time.Now().Add(time.Minute)
		}},
		{"server-only leaf", func(_ *testing.T, _ workloadauthtesting.Workload, c *x509.Certificate) {
			c.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
		}},
		{"other trust domain", func(_ *testing.T, _ workloadauthtesting.Workload, c *x509.Certificate) {
			c.URIs[0].Host = "another-domain"
		}},
		{"deleted subject", func(t *testing.T, w workloadauthtesting.Workload, _ *x509.Certificate) {
			account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: w.Record.Namespace, Name: w.Record.Spec.Subject.ServiceAccount}}
			if err := w.Objects.Delete(t.Context(), account); err != nil {
				t.Fatal(err)
			}
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			workload, leaf := svidWorkload(t)
			test.change(t, workload, leaf)
			resolver, err := auth.NewSVIDResolver(workload.Objects, []string{workload.Sandbox.Namespace}, nil)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := resolver.ResolveVerifiedLeaf(t.Context(), leaf); !errors.Is(err, auth.ErrInvalidBearer) {
				t.Fatalf("authentication = %v, want invalid credential", err)
			}
		})
	}
}

func mustURL(t *testing.T, raw string) *url.URL {
	t.Helper()
	parsed, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func accountUID(t *testing.T, objects client.Client, sandbox *sandboxapi.Sandbox) string {
	t.Helper()
	var account corev1.ServiceAccount
	if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), &account); err != nil {
		t.Fatal(err)
	}
	return string(account.UID)
}

func svidWorkload(t *testing.T) (workloadauthtesting.Workload, *x509.Certificate) {
	t.Helper()
	workload := workloadauthtesting.NewWorkload(t, api, "svid-"+uuid.NewString(), "sandbox")
	leaf := &x509.Certificate{URIs: []*url.URL{mustURL(t, workload.URI)}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}}
	return workload, leaf
}

func svidFixture(t *testing.T) (client.Client, *sandboxapi.Sandbox, *x509.Certificate) {
	t.Helper()
	workload, leaf := svidWorkload(t)
	return workload.Objects, workload.Sandbox, leaf
}
