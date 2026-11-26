package rules

import (
	"context"
	"crypto/tls"
	"errors"
	"net"
	"strings"
	"sync/atomic"
	"testing"

	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/api/rules/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/internal/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/internal/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	grpccredentials "google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// rulesFixture serves the production Rules authentication policy over verified TLS, with live
// WorkloadCredential and Identity reads from envtest. Its second client has no certificate.
type rulesFixture struct {
	client, noCertificate egressrulespb.EgressRulesClient
	reader                *rulesReader
}

type rulesReader struct {
	client.Reader
	unavailable atomic.Bool
}

func (r *rulesReader) Get(ctx context.Context, key client.ObjectKey, object client.Object, options ...client.GetOption) error {
	if r.unavailable.Load() {
		return errors.New("test API server unavailable")
	}
	return r.Reader.Get(ctx, key, object, options...)
}

func rulesWorkload(t *testing.T, name string) workloadauthtesting.Workload {
	t.Helper()
	return workloadauthtesting.NewWorkload(t, kube, "rules-"+uuid.NewString()[:8], name)
}

func serveGRPC(t *testing.T, workload workloadauthtesting.Workload, index *policy.Index) rulesFixture {
	t.Helper()
	ca := transporttesting.NewCA(t)
	serverFiles := ca.Issue(t, peertls.EgressProxyURI)
	config := listener.Config{
		ListenAddress: "127.0.0.1:0",
		TLS:           &listener.TLS{CertFile: serverFiles.CertFile, KeyFile: serverFiles.KeyFile},
		Auth:          []*listener.SPIFFEAuth{{CAFile: serverFiles.CAFile}},
	}
	reader := &rulesReader{Reader: workload.Objects}
	records, err := resolver.New(reader, []string{workload.Record.Namespace}, &resolver.Identities{PolicyNamespace: workload.Identity.Namespace})
	if err != nil {
		t.Fatal(err)
	}
	authentication, err := identity.NewAuthenticator(config, records)
	if err != nil {
		t.Fatal(err)
	}
	credentials, err := config.ServerCredentials(peertls.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	socket, err := net.Listen("tcp", config.ListenAddress)
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{egressrulespb.EgressRules_GetRules_FullMethodName: auth.AnySubject(authentication)}
	server := grpc.NewServer(append(methods.ServerOptions(0), grpc.Creds(credentials))...)
	egressrulespb.RegisterEgressRulesServer(server, NewService(index))
	go func() { _ = server.Serve(socket) }()
	t.Cleanup(server.Stop)
	callerFiles := ca.Issue(t, workload.URI)
	dial := func(certificate bool) egressrulespb.EgressRulesClient {
		clientTLS, err := callerFiles.ClientTLSConfig(peertls.EgressProxyURI)
		if err != nil {
			t.Fatal(err)
		}
		if !certificate {
			clientTLS.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
				return &tls.Certificate{}, nil
			}
		}
		connection, err := grpc.NewClient(socket.Addr().String(), grpc.WithTransportCredentials(grpccredentials.NewTLS(clientTLS)), grpc.WithNoProxy())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = connection.Close() })
		return egressrulespb.NewEgressRulesClient(connection)
	}
	return rulesFixture{client: dial(true), noCertificate: dial(false), reader: reader}
}

func TestGetRulesTellsTheCallerHowToPresentEveryCredential(t *testing.T) {
	workload := rulesWorkload(t, "sb")
	fixture := serveGRPC(t, workload, githubIndex(workload.Identity))

	got, err := fixture.client.GetRules(t.Context(), &egressrulespb.GetRulesRequest{})
	if err != nil {
		t.Fatal(err)
	}

	want := &egressrulespb.GetRulesResponse{
		Caller: &egressrulespb.GetRulesResponse_Identity{Identity: &egressrulespb.IdentitySubject{Name: "sb"}},
		Policies: []*egressrulespb.Policy{{Name: "github", Rules: []*egressrulespb.Rule{
			{
				Hosts:   []string{"api.github.com"},
				Methods: &egressrulespb.StringList{Values: []string{"GET", "POST"}},
				Paths:   &egressrulespb.StringList{Values: []string{"/repos/**"}},
				Credential: &egressrulespb.Credential{
					Name: "github-pat", Description: description, Placeholder: "agentplane-credential-github-pat",
					Targets: []*egressrulespb.Target{
						{Method: egressrulespb.Target_METHOD_SCHEME_TOKEN, Header: proto.String("Authorization"), Scheme: proto.String("Bearer")},
						{Method: egressrulespb.Target_METHOD_BASIC_PASSWORD, Header: proto.String("Authorization")},
					},
				},
			},
			{Hosts: []string{"*.example.com"}},
		}}},
	}
	if !proto.Equal(got, want) {
		t.Errorf("response = %v, want %v", got, want)
	}
	for _, forbidden := range []string{secretValue, "vault-entry", "credential-key", "secretRef", workload.URI} {
		if strings.Contains(got.String(), forbidden) {
			t.Errorf("%q reached a sandbox-readable response", forbidden)
		}
	}
}

func TestGetRulesNamesEachTargetByItsOwnFields(t *testing.T) {
	workload := rulesWorkload(t, "sb")
	index := policy.NewIndex()
	index.Rules.PutIdentity(workload.Identity)
	index.Rules.PutPolicySet(policySet(workload.Identity.Namespace, "all-targets", githubRule))
	index.Rules.PutBinding(binding(workload.Identity.Namespace, "sb", "b", "all-targets"))
	index.PutCredential(resources.Credential{
		Name: "github-pat", Description: description, Source: resources.SecretKey{Name: "github", Key: "token"},
		Targets: []resources.Target{
			resources.WholeValue{Header: "X-Key"},
			resources.SchemeToken{Header: "Authorization", Scheme: "Bearer"},
			resources.BasicUsername{Header: "Authorization"},
			resources.BasicPassword{Header: "Authorization"},
			resources.BasicWhole{Header: "Authorization"},
			resources.JSONField{Field: "password"},
		},
	})
	fixture := serveGRPC(t, workload, index)

	got, err := fixture.client.GetRules(t.Context(), &egressrulespb.GetRulesRequest{})
	if err != nil {
		t.Fatal(err)
	}

	want := []*egressrulespb.Target{
		{Method: egressrulespb.Target_METHOD_WHOLE_VALUE, Header: proto.String("X-Key")},
		{Method: egressrulespb.Target_METHOD_SCHEME_TOKEN, Header: proto.String("Authorization"), Scheme: proto.String("Bearer")},
		{Method: egressrulespb.Target_METHOD_BASIC_USERNAME, Header: proto.String("Authorization")},
		{Method: egressrulespb.Target_METHOD_BASIC_PASSWORD, Header: proto.String("Authorization")},
		{Method: egressrulespb.Target_METHOD_BASIC_WHOLE, Header: proto.String("Authorization")},
		{Method: egressrulespb.Target_METHOD_JSON_FIELD, Field: proto.String("password")},
	}
	targets := got.GetPolicies()[0].GetRules()[0].GetCredential().GetTargets()
	if len(targets) != len(want) {
		t.Fatalf("targets = %v, want %v", targets, want)
	}
	for i := range want {
		if !proto.Equal(targets[i], want[i]) {
			t.Errorf("target %d = %v, want %v", i, targets[i], want[i])
		}
	}
}

func TestGetRulesKeepsAnEmptyListDistinctFromAnAbsentOne(t *testing.T) {
	workload := rulesWorkload(t, "sb")
	index := policy.NewIndex()
	index.Rules.PutIdentity(workload.Identity)
	index.Rules.PutPolicySet(policySet(workload.Identity.Namespace, "none", policyapi.Rule{
		Type: resources.EgressRuleType, Hosts: []string{"a.example.com"}, Methods: []string{}, Paths: []string{},
	}))
	index.Rules.PutBinding(binding(workload.Identity.Namespace, "sb", "b", "none"))
	fixture := serveGRPC(t, workload, index)

	got, err := fixture.client.GetRules(t.Context(), &egressrulespb.GetRulesRequest{})
	if err != nil {
		t.Fatal(err)
	}

	// A rule that admits no method is not one that admits any.
	if rule := got.GetPolicies()[0].GetRules()[0]; rule.Methods == nil || rule.Paths == nil {
		t.Errorf("rule = %v, want empty methods and paths present", rule)
	}
}

func TestGetRulesAnswersNoEgressWithNoPolicies(t *testing.T) {
	workload := rulesWorkload(t, "nobody")
	fixture := serveGRPC(t, workload, githubIndex(workload.Identity))

	got, err := fixture.client.GetRules(t.Context(), &egressrulespb.GetRulesRequest{})
	if err != nil {
		t.Fatal(err)
	}

	want := &egressrulespb.GetRulesResponse{Caller: &egressrulespb.GetRulesResponse_Identity{Identity: &egressrulespb.IdentitySubject{Name: "nobody"}}}
	if !proto.Equal(got, want) {
		t.Errorf("response = %v, want %v", got, want)
	}
}

func TestGetRulesRefusesBearersAndAmbiguousWorkloadCredentials(t *testing.T) {
	workload := rulesWorkload(t, "sb")
	fixture := serveGRPC(t, workload, githubIndex(workload.Identity))
	bearer, err := kube.WorkloadToken(t.Context(), workload.Principal.Account, "retired-egress-rules")
	if err != nil {
		t.Fatal(err)
	}
	for name, test := range map[string]struct {
		certificate bool
		pairs       []string
	}{
		"no credentials":                    {},
		"valid bearer without certificate":  {pairs: []string{"authorization", "Bearer " + bearer}},
		"forged bearer without certificate": {pairs: []string{"authorization", "Bearer forged"}},
		"only a hop bearer":                 {pairs: []string{"proxy-authorization", "Bearer " + bearer}},
		"certificate plus bearer":           {true, []string{"authorization", "Bearer " + bearer}},
		"certificate plus Basic":            {true, []string{"authorization", "Basic abc"}},
		"duplicate authorization":           {true, []string{"authorization", "Bearer " + bearer, "authorization", "Bearer " + bearer}},
		"certificate plus delegation":       {true, []string{auth.DelegationMetadataKey, "forged"}},
	} {
		t.Run(name, func(t *testing.T) {
			caller := fixture.noCertificate
			if test.certificate {
				caller = fixture.client
			}
			ctx := metadata.NewOutgoingContext(t.Context(), metadata.Pairs(test.pairs...))
			got, err := caller.GetRules(ctx, &egressrulespb.GetRulesRequest{})
			if status.Code(err) != codes.Unauthenticated || got != nil {
				t.Fatalf("got %v, %v; want Unauthenticated and no rules", got, err)
			}
			for _, secret := range []string{bearer, "forged"} {
				if strings.Contains(err.Error(), secret) {
					t.Errorf("the response echoes the rejected credential: %v", err)
				}
			}
		})
	}
}

func TestGetRulesRevalidatesTheLiveWorkloadCredential(t *testing.T) {
	workload := rulesWorkload(t, "sb")
	fixture := serveGRPC(t, workload, githubIndex(workload.Identity))
	if _, err := fixture.client.GetRules(t.Context(), &egressrulespb.GetRulesRequest{}); err != nil {
		t.Fatal(err)
	}
	workload.Revoke(t)
	if _, err := fixture.client.GetRules(t.Context(), &egressrulespb.GetRulesRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("revoked credential returned %v, want Unauthenticated", err)
	}
}

func TestGetRulesIsUnavailableWhenTheLiveCredentialCannotBeRead(t *testing.T) {
	workload := rulesWorkload(t, "sb")
	fixture := serveGRPC(t, workload, githubIndex(workload.Identity))
	if _, err := fixture.client.GetRules(t.Context(), &egressrulespb.GetRulesRequest{}); err != nil {
		t.Fatal(err)
	}
	fixture.reader.unavailable.Store(true)
	if _, err := fixture.client.GetRules(t.Context(), &egressrulespb.GetRulesRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("got %v, want Unavailable: an unreadable credential says nothing about the caller", err)
	}
}
