package environments

import (
	"net"
	"testing"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environmentaccess"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Exercise certificate admission and the guarded write against the real envtest API server.
// Even an actor with a separate direct operator grant cannot lend it to its effective Identity.
func TestServiceDelegatedResumeUsesEffectiveIdentityGrant(t *testing.T) {
	for _, tc := range []struct {
		name     string
		other    bool
		grant    bool
		token    bool
		allowOwn bool
		wantCode codes.Code
	}{
		{name: "granted Identity", grant: true, token: true, allowOwn: true},
		{name: "other Identity", other: true, grant: true, token: true, allowOwn: true, wantCode: codes.NotFound},
		{name: "no resume grant", token: true, allowOwn: true, wantCode: codes.PermissionDenied},
		{name: "actor cannot waive token", grant: true, allowOwn: true, wantCode: codes.InvalidArgument},
		{name: "explicit grant independent of automatic wake opt-in", grant: true, token: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scheme := runtime.NewScheme()
			for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, sandboxapi.AddToScheme, policyapi.AddToScheme} {
				if err := add(scheme); err != nil {
					t.Fatal(err)
				}
			}
			objects, err := client.NewWithWatch(watchAPI.Config, client.Options{Scheme: scheme})
			if err != nil {
				t.Fatal(err)
			}
			namespace := "test-delegated-resume-" + uuid.NewString()
			if err := objects.Create(t.Context(), &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
				t.Fatal(err)
			}
			identity := &policyapi.Identity{
				ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: "test-owner"},
				Spec:       policyapi.IdentitySpec{Description: "Delegated resume test owner"},
			}
			other := &policyapi.Identity{
				ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: "test-other"},
				Spec:       policyapi.IdentitySpec{Description: "Delegated resume test other Identity"},
			}
			for _, object := range []client.Object{identity, other} {
				if err := objects.Create(t.Context(), object); err != nil {
					t.Fatal(err)
				}
			}
			sandbox := sandboxObject("")
			sandbox.Namespace = namespace
			sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: identity.Name}
			sandbox.Spec.WakeGeneration = 3
			sandbox.Spec.WakeOnSend = true
			sandbox.Spec.ResumePolicy.AllowOwnServiceAccount = tc.allowOwn
			if err := objects.Create(t.Context(), sandbox); err != nil {
				t.Fatal(err)
			}
			idle := sandboxapi.SuspendedByIdle
			sandbox.Status = sandboxapi.SandboxStatus{
				OperatingMode: sandboxapi.OperatingModeSuspended, StopStage: sandboxapi.StopStageStopped,
				SuspendedBy: &idle, SuspendedAtWakeGeneration: proto.Int64(3),
			}
			if err := objects.Status().Update(t.Context(), sandbox); err != nil {
				t.Fatal(err)
			}
			rules := environmentaccess.NewIndex()
			caller := identity
			if tc.other {
				caller = other
			}
			rules.PutIdentity(caller)
			if tc.grant {
				rules.PutPolicySet(&policyapi.PolicySet{
					ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: "test-resume"},
					Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{
						Type: "environment", Operations: []string{"resume"},
						Environments: []policyapi.EnvironmentSelector{{BoundIdentity: &policyapi.BoundIdentitySelector{Self: true}}},
					}}},
				})
				rules.PutBinding(&policyapi.PolicyBinding{
					ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: "test-binding"},
					Spec:       policyapi.PolicyBindingSpec{Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: caller.Name}}, PolicySets: []string{"test-resume"}},
				})
			}
			manager, err := lifecycle.NewManager(lifecycle.Options{
				Namespace: namespace, Objects: objects, WakeBudget: unusedWakeBudget{},
				EnvironmentRules: rules, PolicyReady: func() error { return nil },
				OperatorServiceCallers: []string{peertls.ActionsURI},
			})
			if err != nil {
				t.Fatal(err)
			}
			service, err := New(namespace, objects, runtimeconfig.Catalog{}, manager, nil, ManagementOptions{})
			if err != nil {
				t.Fatal(err)
			}
			identities, err := auth.KubernetesIdentityUIDs(objects, namespace)
			if err != nil {
				t.Fatal(err)
			}
			methods, err := WithServiceCallers(Methods(nil), nil, nil, nil, []string{peertls.ActionsURI})
			if err != nil {
				t.Fatal(err)
			}
			methods, err = methods.WithServiceDelegation([]string{environmentpb.EnvironmentService_ResumeEnvironment_FullMethodName}, []auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}}, identities)
			if err != nil {
				t.Fatal(err)
			}
			ca := peertlstesting.NewCA(t)
			serverFiles := ca.Issue(t, peertls.SessionDispatcherURI)
			serverTLS, err := serverFiles.WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
			if err != nil {
				t.Fatal(err)
			}
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
			environmentpb.RegisterEnvironmentServiceServer(server, service)
			go func() { _ = server.Serve(listener) }()
			t.Cleanup(server.Stop)
			files := ca.Issue(t, peertls.ActionsURI)
			clientTLS, err := files.ClientTLSConfig(peertls.SessionDispatcherURI)
			if err != nil {
				t.Fatal(err)
			}
			connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)))
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = connection.Close() })
			target := &identitypb.EnvironmentTarget{Namespace: namespace, Name: sandbox.Name, Uid: string(sandbox.UID)}
			// Automatic wake has a separate policy: matching execution Identity plus opt-in.
			// Explicit resume below continues to require its current environment grant.
			wake, err := manager.AssessAutomaticWake(t.Context(), target, lifecycle.IdentityWakeCaller{
				Identity: &identitypb.Identity{Name: caller.Name, Uid: string(caller.UID)},
			})
			wantWakeState := environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE
			wantWakeReason := environmentlifecyclepb.WakeReason_WAKE_REASON_UNSPECIFIED
			if tc.other {
				wantWakeState = environmentlifecyclepb.WakeState_WAKE_STATE_DENIED
				wantWakeReason = environmentlifecyclepb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED
			} else if !tc.allowOwn {
				wantWakeState = environmentlifecyclepb.WakeState_WAKE_STATE_DENIED
				wantWakeReason = environmentlifecyclepb.WakeReason_WAKE_REASON_RESUME_POLICY_DISABLED
			}
			if err != nil || wake.GetState() != wantWakeState || wake.GetReason() != wantWakeReason {
				t.Fatalf("automatic wake = %v, %v; want %v/%v", wake, err, wantWakeState, wantWakeReason)
			}
			request := &environmentpb.ResumeEnvironmentRequest{Target: target}
			if tc.token {
				request.ResumeToken = lifecycle.NewResumeToken(target, 3)
			}
			response, err := environmentpb.NewEnvironmentServiceClient(connection).ResumeEnvironment(
				serviceDelegationContext(t, auth.Identity{Name: caller.Name, UID: string(caller.UID)}), request,
			)
			if status.Code(err) != tc.wantCode {
				t.Fatalf("ResumeEnvironment = %v, %v; want %v", response, err, tc.wantCode)
			}
			var current sandboxapi.Sandbox
			if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), &current); err != nil {
				t.Fatal(err)
			}
			wantGeneration := int64(3)
			if tc.wantCode == codes.OK {
				wantGeneration++
				if response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED || response.GetEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_IDENTITY_GRANT {
					t.Fatalf("resume did not use effective Identity grant: %v", response)
				}
			}
			if current.Spec.WakeGeneration != wantGeneration {
				t.Fatalf("wake generation = %d, want %d", current.Spec.WakeGeneration, wantGeneration)
			}
		})
	}
}
