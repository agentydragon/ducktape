package service

import (
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/access"
	"github.com/agentydragon/ducktape/agentplane/environments/internal/resume"
	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/testscheme"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
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
			objects, err := client.NewWithWatch(watchAPI.Config, client.Options{Scheme: testscheme.New(t)})
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
			rules := access.NewIndex()
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
			manager, err := resume.NewManager(lifecycle.Options{
				Namespace: namespace, Objects: objects, Identities: kubernetesIdentities(t, objects, namespace),
				EnvironmentRules: rules, PolicyReady: func() error { return nil },
				OperatorServiceCallers: []string{peertls.ActionsURI},
			})
			if err != nil {
				t.Fatal(err)
			}
			service, err := New(namespace, objects, kubernetesIdentities(t, objects, namespace), runtimeconfig.Catalog{}, manager, Options{})
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
			connection := grpcmethods.ServeTLSWithTimeout(t, time.Second, peertls.SessionDispatcherURI, methods, func(server *grpc.Server) {
				environmentpb.RegisterEnvironmentServiceServer(server, service)
			}, peertls.ActionsURI)
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
			response, err := environmentpb.NewEnvironmentServiceClient(grpcmethods.AsCaller(connection, peertls.ActionsURI)).ResumeEnvironment(
				grpcmethods.WithActionDelegation(t, t.Context(), auth.Identity{Name: caller.Name, UID: string(caller.UID)}), request,
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

// Managers create directly; only launchers may attach an Action launch origin. Both roles
// must reach the handler over mTLS without acquiring one another's authority.
func TestServiceManagerCreateAdmission(t *testing.T) {
	objects, err := client.NewWithWatch(watchAPI.Config, client.Options{Scheme: testscheme.New(t)})
	if err != nil {
		t.Fatal(err)
	}
	namespace := "test-manager-create-" + uuid.NewString()
	identity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: "owner"}, Spec: policyapi.IdentitySpec{Description: "Service manager create test"}}
	for _, object := range []client.Object{&corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}, identity} {
		if err := objects.Create(t.Context(), object); err != nil {
			t.Fatal(err)
		}
	}
	manager, err := resume.NewManager(lifecycle.Options{Namespace: namespace, Objects: objects, Identities: kubernetesIdentities(t, objects, namespace)})
	if err != nil {
		t.Fatal(err)
	}
	service, err := New(namespace, objects, kubernetesIdentities(t, objects, namespace), runtimeconfig.Catalog{}, manager, Options{
		ServiceManagers: sets.New(peertls.AppURI), ServiceLaunchers: sets.New(peertls.ActionsURI),
	})
	if err != nil {
		t.Fatal(err)
	}
	methods, err := WithServiceCallers(Methods(nil), []string{peertls.NotificationsURI}, []string{peertls.AppURI}, []string{peertls.ActionsURI}, nil)
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.ServeTLSWithTimeout(t, time.Second, peertls.SessionDispatcherURI, methods, func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, service)
	}, peertls.AppURI, peertls.ActionsURI, peertls.NotificationsURI, peertls.CentralSamplerURI)
	dial := func(serviceID string) environmentpb.EnvironmentServiceClient {
		t.Helper()
		return environmentpb.NewEnvironmentServiceClient(grpcmethods.AsCaller(connection, serviceID))
	}
	for _, tc := range []struct {
		name, actor string
		origin      bool
		code        codes.Code
	}{
		{name: "manager-direct", actor: peertls.AppURI},
		{name: "manager-action-origin", actor: peertls.AppURI, origin: true, code: codes.PermissionDenied},
		{name: "launcher-direct", actor: peertls.ActionsURI, code: codes.PermissionDenied},
		{name: "launcher-action-origin", actor: peertls.ActionsURI, origin: true},
		{name: "reader-direct", actor: peertls.NotificationsURI, code: codes.PermissionDenied},
		{name: "ungranted-direct", actor: peertls.CentralSamplerURI, code: codes.PermissionDenied},
	} {
		t.Run(tc.name, func(t *testing.T) {
			request := &environmentpb.CreateEnvironmentRequest{Name: tc.name, Template: "test", Identity: identity.Name}
			if tc.origin {
				request.LaunchOrigin = &environmentpb.EnvironmentLaunchOrigin{CallerIdentity: &identitypb.Identity{Name: identity.Name, Uid: string(identity.UID)}, ActionGroup: "test"}
			}
			rpc := dial(tc.actor)
			created, err := rpc.CreateEnvironment(t.Context(), request)
			if status.Code(err) != tc.code {
				t.Fatalf("create = %v, %v; want %v", created, err, tc.code)
			}
			var stored sandboxapi.Sandbox
			readErr := objects.Get(t.Context(), types.NamespacedName{Namespace: namespace, Name: tc.name}, &stored)
			if tc.code != codes.OK {
				if !apierrors.IsNotFound(readErr) {
					t.Fatalf("denied create wrote a Sandbox: %v", readErr)
				}
				return
			}
			if readErr != nil || created.GetUid() == "" || created.GetUid() != string(stored.UID) {
				t.Fatalf("persisted create = %v, %v; target %v", stored.UID, readErr, created)
			}
			retried, err := rpc.CreateEnvironment(t.Context(), request)
			if err != nil || !proto.Equal(created, retried) {
				t.Fatalf("idempotent retry = %v, %v; want %v", retried, err, created)
			}
		})
	}
}
