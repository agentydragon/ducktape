package environments

import (
	"errors"
	"testing"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environmentaccess"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestIdentityResumeProjectionAndDelegatedAdmissionFollowCurrentGrant(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: "test-agent"}
	identity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: "test-agent", UID: "test-agent-uid"}}
	objects := readerWithObjects(t, sandbox, identity)
	rules := environmentaccess.NewIndex()
	rules.PutIdentity(identity)
	var policyError error
	manager, err := lifecycle.NewManager(lifecycle.Options{
		Namespace: discoveryNamespace, Objects: objects, WakeBudget: unusedWakeBudget{},
		EnvironmentRules: rules, PolicyReady: func() error { return policyError },
	})
	if err != nil {
		t.Fatal(err)
	}
	rpc := serveIdentityDiscoveryWithLifecycle(t, objects, runtimeconfig.Catalog{}, nil, manager)
	ctx := delegatedIdentityBearing(t, actionsToken, auth.Identity{Name: identity.Name, UID: string(identity.UID)})
	target := &identitypb.EnvironmentTarget{Namespace: sandbox.Namespace, Name: sandbox.Name, Uid: string(sandbox.UID)}
	get := &environmentpb.GetEnvironmentRequest{Target: target}
	view, err := rpc.GetEnvironment(ctx, get)
	if err != nil || view.GetActions().GetResumeEligibility() != nil || view.GetActions().GetResumeToken() != "" {
		t.Fatalf("ungranted projection = %v, %v", view, err)
	}
	token := lifecycle.NewResumeToken(target, sandbox.Spec.WakeGeneration)
	request := &environmentpb.ResumeEnvironmentRequest{Target: target, ResumeToken: token}
	if _, err := rpc.ResumeEnvironment(ctx, request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ungranted own resume = %v", err)
	}
	rules.PutPolicySet(&policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Name: "test-resume"}, Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: "environment", Operations: []string{"resume"}, Environments: []policyapi.EnvironmentSelector{{BoundIdentity: &policyapi.BoundIdentitySelector{Self: true}}}}}}})
	rules.PutBinding(&policyapi.PolicyBinding{ObjectMeta: metav1.ObjectMeta{Name: "test-binding"}, Spec: policyapi.PolicyBindingSpec{Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: identity.Name}}, PolicySets: []string{"test-resume"}}})
	view, err = rpc.GetEnvironment(ctx, get)
	if err != nil || view.GetActions().GetResumeToken() != token || view.GetActions().GetWakeGeneration() != sandbox.Spec.WakeGeneration {
		t.Fatalf("granted projection = %v, %v", view, err)
	}
	if _, err := rpc.ResumeEnvironment(ctx, request); err != nil {
		t.Fatalf("granted delegated resume = %v", err)
	}
	rules.DeleteBinding("test-binding")
	view, err = rpc.GetEnvironment(ctx, get)
	if err != nil || view.GetActions().GetResumeToken() != "" || view.GetActions().GetResumeEligibility() != nil {
		t.Fatalf("revoked projection = %v, %v", view, err)
	}
	if _, err := rpc.ResumeEnvironment(ctx, request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("revoked resume = %v", err)
	}
	policyError = errors.New("test policy watch unavailable")
	view, err = rpc.GetEnvironment(ctx, get)
	if err != nil || view.GetActions().GetResumeToken() != "" {
		t.Fatalf("self-read during policy failure = %v, %v", view, err)
	}
	if _, err := rpc.ResumeEnvironment(ctx, request); status.Code(err) != codes.Unavailable {
		t.Fatalf("resume during policy failure = %v", err)
	}
}
