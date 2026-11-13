package main

import (
	"context"
	"fmt"
	"net"
	"strings"
	"testing"

	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/api/rules/egress_rulespb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
)

// rulesWith is ProtoJSON for one rule that carries a credential with these targets.
func rulesWith(targets string) string {
	return fmt.Sprintf(`{"subject":{"namespace":"test-ns","name":"test-sandbox"},"policies":[{"name":"test-policy","rules":[
		{"hosts":["api.example.test"],"methods":{"values":["GET"]},"credential":{"name":"test-credential","description":"d","placeholder":"agentplane-credential-test-credential","targets":%s}},
		{"hosts":["open.example.test"],"credential":null}]}]}`, targets)
}

func TestRulesTextShowsHowToPresentTheCredential(t *testing.T) {
	const rule = "test-policy  api.example.test  GET  *  credential test-credential: send "
	const openRule = "test-policy  open.example.test  *  *"
	cases := []struct{ name, targets, want string }{
		{
			"scheme token", `[{"method":"METHOD_SCHEME_TOKEN","header":"Authorization","scheme":"Bearer"}]`,
			"Authorization: Bearer agentplane-credential-test-credential",
		},
		{
			"whole header value", `[{"method":"METHOD_WHOLE_VALUE","header":"X-Test-Key"}]`,
			"X-Test-Key: agentplane-credential-test-credential",
		},
		{
			"basic username", `[{"method":"METHOD_BASIC_USERNAME","header":"Authorization"}]`,
			"Authorization: Basic with the username agentplane-credential-test-credential",
		},
		{
			"basic password", `[{"method":"METHOD_BASIC_PASSWORD","header":"Authorization"}]`,
			"Authorization: Basic with the password agentplane-credential-test-credential",
		},
		{
			"basic whole", `[{"method":"METHOD_BASIC_WHOLE","header":"Authorization"}]`,
			"Authorization: Basic with the whole user:password agentplane-credential-test-credential",
		},
		{
			"JSON field", `[{"method":"METHOD_JSON_FIELD","field":"token"}]`,
			`the top-level JSON field "token" as agentplane-credential-test-credential`,
		},
		{
			"several targets are alternatives",
			`[{"method":"METHOD_SCHEME_TOKEN","header":"Authorization","scheme":"Bearer"},{"method":"METHOD_WHOLE_VALUE","header":"X-Test-Key"}]`,
			"Authorization: Bearer agentplane-credential-test-credential  or  X-Test-Key: agentplane-credential-test-credential",
		},
		{
			"a method from a newer service is named", `[{"method":"METHOD_FUTURE"}]`,
			"METHOD_FUTURE agentplane-credential-test-credential (see --json)",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			stdout, err := rulesText([]byte(rulesWith(tc.targets)))
			want := "subject test-ns/test-sandbox\n" + rule + tc.want + "  d\n" + openRule
			if err != nil || strings.TrimSpace(stdout) != want {
				t.Errorf("stdout %q, error %v; want %q", stdout, err, want)
			}
		})
	}
}

func TestRulesTextNamesAnIdentityCaller(t *testing.T) {
	stdout, err := rulesText([]byte(`{"identity":{"name":"test-agent"},"policies":[{"name":"test-policy","rules":[{"hosts":["open.example.test"]}]}]}`))
	if want := "identity test-agent\ntest-policy  open.example.test  *  *"; err != nil || strings.TrimSpace(stdout) != want {
		t.Errorf("stdout %q, error %v; want %q", stdout, err, want)
	}
}

// egressFake records the actual gRPC metadata received from the CLI.
type egressFake struct {
	egressrulespb.UnimplementedEgressRulesServer
	response      *egressrulespb.GetRulesResponse
	authorization []string
	err           error
}

func (f *egressFake) GetRules(ctx context.Context, _ *egressrulespb.GetRulesRequest) (*egressrulespb.GetRulesResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	f.authorization = md.Get("authorization")
	return f.response, f.err
}

func newEgressFake(t *testing.T) (*egressFake, string) {
	t.Helper()
	for _, variable := range []string{"HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"} {
		t.Setenv(variable, "")
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	fake := &egressFake{response: &egressrulespb.GetRulesResponse{}}
	egressrulespb.RegisterEgressRulesServer(server, fake)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { server.Stop(); _ = listener.Close() })
	return fake, "http://" + listener.Addr().String()
}

func TestEgressRulesUsesGRPCAndDisplaysTheResponse(t *testing.T) {
	fake, endpoint := newEgressFake(t)
	if err := protojson.Unmarshal([]byte(rulesWith(`[]`)), fake.response); err != nil {
		t.Fatal(err)
	}
	code, output, stderr := invoke(egressEnv(endpoint), "egress", "rules", "--json")
	if code != 0 || decodeObject(t, output)["subject"] == nil || strings.Join(fake.authorization, ",") != "Bearer "+testCredential {
		t.Fatalf("exit %d, output %q, stderr %q, authorization %v", code, output, stderr, fake.authorization)
	}
	code, output, _ = invoke(egressEnv(endpoint), "egress", "rules")
	if code != 0 || !strings.Contains(output, "api.example.test") {
		t.Errorf("exit %d, output %q", code, output)
	}
	fake.response.Policies = nil
	code, output, _ = invoke(egressEnv(endpoint), "egress", "rules", "--json")
	if code != 0 || !strings.Contains(output, `"policies":[]`) {
		t.Errorf("empty rules: exit %d, output %q", code, output)
	}
	fake.err = status.Error(codes.PermissionDenied, "refused")
	code, _, _ = invoke(egressEnv(endpoint), "egress", "rules")
	if code != exitRefused {
		t.Errorf("refusal exit %d", code)
	}
}

func TestEgressGuidesAreAvailableWithoutServiceConfiguration(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want string
	}{
		{[]string{"egress", "guide"}, "# Agentplane egress"},
		{[]string{"egress", "guide", "credentials"}, "# Use credentials through the proxy"},
	} {
		code, stdout, stderr := invoke(nil, tc.args...)
		if code != 0 || !strings.Contains(stdout, tc.want) {
			t.Errorf("%v: exit %d, stdout %q, stderr %q", tc.args, code, stdout, stderr)
		}
	}
	code, _, _ := invoke(nil, "egress", "guide", "unknown")
	if code != 2 {
		t.Errorf("unknown guide: exit %d, want 2", code)
	}
}
