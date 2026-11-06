package rules

import (
	"context"
	"net"
	"strings"
	"testing"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

// serveGRPC invokes the production service with a test-only admitted caller. Transport SVID
// verification is exercised by workload_auth/facade and the real-proxy harness.
func serveGRPC(t *testing.T, account string, index *policy.Index) egressrulespb.EgressRulesClient {
	t.Helper()
	service := NewService(index)
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer(grpc.UnaryInterceptor(func(ctx context.Context, request any, _ *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		return handler(auth.WithCaller(ctx, auth.Principal{Account: auth.Account{Namespace: namespace, Name: account}}), request)
	}))
	egressrulespb.RegisterEgressRulesServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(
		"passthrough:///egress-rules-test",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return egressrulespb.NewEgressRulesClient(connection)
}

func TestGetRulesTellsTheCallerHowToPresentEveryCredential(t *testing.T) {
	client := serveGRPC(t, "sb", githubIndex())

	got, err := client.GetRules(context.Background(), &egressrulespb.GetRulesRequest{})
	if err != nil {
		t.Fatal(err)
	}

	want := &egressrulespb.GetRulesResponse{
		Subject: &identitypb.ServiceAccount{Namespace: namespace, Name: "sb"},
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
	for _, forbidden := range []string{secretValue, "vault-entry", "credential-key", "secretRef", "proxy-hop-token"} {
		if strings.Contains(got.String(), forbidden) {
			t.Errorf("%q reached a sandbox-readable response", forbidden)
		}
	}
}

func TestGetRulesNamesEachTargetByItsOwnFields(t *testing.T) {
	index := policy.NewIndex()
	index.Rules.PutPolicySet(policySet("all-targets", githubRule))
	index.Rules.PutBinding(binding("sb", "b", "all-targets"))
	index.PutCredential(resources.Credential{
		Name: "github-pat", Description: description, Source: resources.AuthenticatedWorkloadToken{},
		Targets: []resources.Target{
			resources.WholeValue{Header: "X-Key"},
			resources.SchemeToken{Header: "Authorization", Scheme: "Bearer"},
			resources.BasicUsername{Header: "Authorization"},
			resources.BasicPassword{Header: "Authorization"},
			resources.BasicWhole{Header: "Authorization"},
			resources.JSONField{Field: "password"},
		},
	})
	client := serveGRPC(t, "sb", index)

	got, err := client.GetRules(context.Background(), &egressrulespb.GetRulesRequest{})
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
	index := policy.NewIndex()
	index.Rules.PutPolicySet(policySet("none", policyapi.Rule{
		Type: resources.EgressRuleType, Hosts: []string{"a.example.com"}, Methods: []string{}, Paths: []string{},
	}))
	index.Rules.PutBinding(binding("sb", "b", "none"))
	client := serveGRPC(t, "sb", index)

	got, err := client.GetRules(context.Background(), &egressrulespb.GetRulesRequest{})
	if err != nil {
		t.Fatal(err)
	}

	// A rule that admits no method is not one that admits any.
	if rule := got.GetPolicies()[0].GetRules()[0]; rule.Methods == nil || rule.Paths == nil {
		t.Errorf("rule = %v, want empty methods and paths present", rule)
	}
}

func TestGetRulesAnswersNoEgressWithNoPolicies(t *testing.T) {
	client := serveGRPC(t, "nobody", githubIndex())

	got, err := client.GetRules(context.Background(), &egressrulespb.GetRulesRequest{})
	if err != nil {
		t.Fatal(err)
	}

	want := &egressrulespb.GetRulesResponse{Subject: &identitypb.ServiceAccount{Namespace: namespace, Name: "nobody"}}
	if !proto.Equal(got, want) {
		t.Errorf("response = %v, want %v", got, want)
	}
}

func TestGetRulesRequiresAnAdmittedSVIDCaller(t *testing.T) {
	service := NewService(githubIndex())
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs(
		"authorization", "Bearer proxy-hop-token",
		"proxy-authorization", "Bearer proxy-hop-token",
	))
	got, err := service.GetRules(ctx, &egressrulespb.GetRulesRequest{})
	if status.Code(err) != codes.Unauthenticated || got != nil {
		t.Errorf("got %v, %v; want Unauthenticated without an admitted SVID caller", got, err)
	}
}
