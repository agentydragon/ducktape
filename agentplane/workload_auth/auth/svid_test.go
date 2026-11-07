package auth_test

import (
	"crypto/x509"
	"errors"
	"net/url"
	"testing"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestSVIDResolvesTheLiveSandboxExecutionAccount(t *testing.T) {
	for _, kind := range []sandboxapi.RunnerCredentialKind{sandboxapi.RunnerCredentialKindPod, sandboxapi.RunnerCredentialKindVM} {
		t.Run(string(kind), func(t *testing.T) {
			objects, sandbox, leaf := svidFixture(t)
			sandbox.Status.RunnerCredential.Kind = kind
			if kind == sandboxapi.RunnerCredentialKindVM {
				sandbox.Status.RunnerCredential.PodUID = ""
				sandbox.Status.RunnerCredential.LastVMIUID = "vmi-uid"
			}
			if err := objects.Status().Update(t.Context(), sandbox); err != nil {
				t.Fatal(err)
			}
			resolver, err := auth.NewSVIDResolver(objects, []string{sandbox.Namespace})
			if err != nil {
				t.Fatal(err)
			}
			got, err := resolver.ResolveVerifiedLeaf(t.Context(), leaf)
			if err != nil {
				t.Fatal(err)
			}
			want := auth.Principal{Account: auth.Account{Namespace: sandbox.Namespace, Name: sandbox.Name}, ServiceAccountUID: accountUID(t, objects, sandbox), PodName: sandbox.Status.Incarnation.Name, PodUID: sandbox.Status.Incarnation.UID}
			if got != want {
				t.Fatalf("principal = %#v, want %#v", got, want)
			}
		})
	}
}

func TestSVIDRefusesRevokedOrIncompleteWorkloads(t *testing.T) {
	for _, test := range []struct {
		name   string
		change func(*sandboxapi.Sandbox, *x509.Certificate)
	}{
		{"replaced Sandbox", func(s *sandboxapi.Sandbox, c *x509.Certificate) {
			c.URIs[0].Path = "/environment-runtime/" + s.Namespace + "/replaced/" + s.Status.RunnerCredential.ID
		}},
		{"old credential", func(s *sandboxapi.Sandbox, _ *x509.Certificate) {
			s.Status.RunnerCredential.ID = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
		}},
		{"unaccepted credential", func(s *sandboxapi.Sandbox, _ *x509.Certificate) { s.Status.RunnerCredential.AcceptedID = "" }},
		{"missing binding", func(s *sandboxapi.Sandbox, _ *x509.Certificate) { s.Status.RunnerCredential = nil }},
		{"missing Pod binding", func(s *sandboxapi.Sandbox, _ *x509.Certificate) { s.Status.RunnerCredential.PodUID = "" }},
		{"different Pod", func(s *sandboxapi.Sandbox, _ *x509.Certificate) { s.Status.Incarnation.UID = "replacement-pod" }},
		{"missing incarnation", func(s *sandboxapi.Sandbox, _ *x509.Certificate) { s.Status.Incarnation = nil }},
		{"expired leaf", func(_ *sandboxapi.Sandbox, c *x509.Certificate) { c.NotAfter = time.Now().Add(-time.Minute) }},
		{"not yet valid leaf", func(_ *sandboxapi.Sandbox, c *x509.Certificate) { c.NotBefore = time.Now().Add(time.Minute) }},
		{"server-only leaf", func(_ *sandboxapi.Sandbox, c *x509.Certificate) {
			c.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
		}},
		{"other trust domain", func(_ *sandboxapi.Sandbox, c *x509.Certificate) { c.URIs[0].Host = "another-domain" }},
	} {
		t.Run(test.name, func(t *testing.T) {
			objects, sandbox, leaf := svidFixture(t)
			test.change(sandbox, leaf)
			if err := objects.Status().Update(t.Context(), sandbox); err != nil {
				t.Fatal(err)
			}
			resolver, err := auth.NewSVIDResolver(objects, []string{sandbox.Namespace})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := resolver.ResolveVerifiedLeaf(t.Context(), leaf); !errors.Is(err, auth.ErrInvalidBearer) {
				t.Fatalf("authentication = %v, want invalid credential", err)
			}
		})
	}
}

func TestSVIDRefusesDeletedSandboxAndReplacedAccount(t *testing.T) {
	for _, remove := range []bool{true, false} {
		objects, sandbox, leaf := svidFixture(t)
		if remove {
			if err := objects.Delete(t.Context(), sandbox); err != nil {
				t.Fatal(err)
			}
		} else {
			var account corev1.ServiceAccount
			if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), &account); err != nil {
				t.Fatal(err)
			}
			account.OwnerReferences[0].UID = "replacement"
			if err := objects.Update(t.Context(), &account); err != nil {
				t.Fatal(err)
			}
		}
		resolver, err := auth.NewSVIDResolver(objects, []string{sandbox.Namespace})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := resolver.ResolveVerifiedLeaf(t.Context(), leaf); !errors.Is(err, auth.ErrInvalidBearer) {
			t.Fatalf("authentication = %v, want invalid credential", err)
		}
	}
}

func accountUID(t *testing.T, objects client.Client, sandbox *sandboxapi.Sandbox) string {
	t.Helper()
	var account corev1.ServiceAccount
	if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), &account); err != nil {
		t.Fatal(err)
	}
	return string(account.UID)
}

func svidFixture(t *testing.T) (client.Client, *sandboxapi.Sandbox, *x509.Certificate) {
	t.Helper()
	workload := workloadauthtesting.NewWorkload(t, api, "svid-"+uuid.NewString(), "sandbox")
	uri, err := url.Parse(workload.URI)
	if err != nil {
		t.Fatal(err)
	}
	leaf := &x509.Certificate{URIs: []*url.URL{uri}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}}
	return workload.Objects, workload.Sandbox, leaf
}
