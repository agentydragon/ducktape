package environments

import (
	"context"
	"crypto/x509"
	"fmt"
	"net"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// serveIdentityEnvironments serves namespace's EnvironmentService over a TLS listener that admits
// workload SVIDs, resolving Identity subjects in namespace, and returns a client for a workload URI.
func serveIdentityEnvironments(t *testing.T, namespace string, objects client.WithWatch) func(uri string) environmentpb.EnvironmentServiceClient {
	t.Helper()
	svids, err := auth.NewSVIDResolver(objects, []string{namespace}, &resolver.Identities{PolicyNamespace: namespace})
	if err != nil {
		t.Fatal(err)
	}
	tokens, err := auth.NewResolver(watchAPI.TokenReviews(), dispatcherAudience, []string{namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	ca := transporttesting.NewCA(t)
	serverCredential := ca.IssueWithUsage(t, environment_runtime_transport.SessionDispatcherURI, x509.ExtKeyUsageServerAuth)
	listenerConfig := listener.Config{
		ListenAddress: ":8080",
		TLS:           &listener.TLS{CertFile: serverCredential.CertFile, KeyFile: serverCredential.KeyFile},
		Auth:          []listener.Authentication{&listener.SPIFFEAuth{CAFile: ca.RootFile}},
	}
	serverTLS, err := listenerConfig.TLSConfig(environment_runtime_transport.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS.NextProtos = []string{"h2"}
	workloadAuth, err := tokens.WithListenerAuthentication(listenerConfig, svids)
	if err != nil {
		t.Fatal(err)
	}
	manager, err := lifecycle.NewManager(lifecycle.Options{Namespace: namespace, Objects: objects, WakeBudget: unusedWakeBudget{}})
	if err != nil {
		t.Fatal(err)
	}
	service, err := New(namespace, objects, runtimeconfig.Catalog{}, manager, nil, ManagementOptions{})
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(Methods(workloadAuth).ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	environmentpb.RegisterEnvironmentServiceServer(server, service)
	netListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(netListener) }()
	t.Cleanup(server.Stop)
	return func(uri string) environmentpb.EnvironmentServiceClient {
		clientTLS, err := ca.Issue(t, uri).ClientTLSConfig(environment_runtime_transport.SessionDispatcherURI)
		if err != nil {
			t.Fatal(err)
		}
		connection, err := grpc.NewClient(netListener.Addr().String(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)), grpc.WithNoProxy())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = connection.Close() })
		return environmentpb.NewEnvironmentServiceClient(connection)
	}
}

func identityTestObjects(t *testing.T) client.WithWatch {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, sandboxapi.AddToScheme, policyapi.AddToScheme, workloadapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	objects, err := client.NewWithWatch(watchAPI.Config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	return objects
}

// nameIdentity sets the Sandbox's spec.identity to identity, or clears it for "".
func nameIdentity(t *testing.T, objects client.Client, sandbox *sandboxapi.Sandbox, identity string) {
	t.Helper()
	if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), sandbox); err != nil {
		t.Fatal(err)
	}
	sandbox.Spec.Identity = nil
	if identity != "" {
		sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: identity}
	}
	if err := objects.Update(t.Context(), sandbox); err != nil {
		t.Fatal(err)
	}
}

func targetOf(sandbox *sandboxapi.Sandbox) *identitypb.EnvironmentTarget {
	return &identitypb.EnvironmentTarget{Namespace: sandbox.Namespace, Name: sandbox.Name, Uid: string(sandbox.UID)}
}

func listedNames(t *testing.T, ctx context.Context, rpc environmentpb.EnvironmentServiceClient, limit uint32) []string {
	t.Helper()
	var names []string
	token := ""
	for {
		page, err := rpc.ListEnvironments(ctx, &environmentpb.ListEnvironmentsRequest{Limit: limit, PageToken: token})
		if err != nil {
			t.Fatalf("ListEnvironments: %v", err)
		}
		for _, item := range page.GetItems() {
			names = append(names, item.GetTarget().GetName())
		}
		if token = page.GetNextPageToken(); token == "" {
			return names
		}
	}
}

func watchedNames(t *testing.T, ctx context.Context, rpc environmentpb.EnvironmentServiceClient) []string {
	t.Helper()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := rpc.WatchEnvironments(ctx, &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatalf("WatchEnvironments ended before its snapshot completed: %v", err)
		}
		if frame.GetSnapshotComplete() != nil {
			slices.Sort(names)
			return names
		}
		for _, item := range frame.GetSnapshot().GetEnvironments() {
			names = append(names, item.GetTarget().GetName())
		}
	}
}

func TestIdentityCallerReadsAndResumesOnlyTheEnvironmentsNamingIt(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	namespace := fmt.Sprintf("test-environment-identity-%d", watchNamespaces.Add(1))
	objects := identityTestObjects(t)
	// Identities live in the Sandboxes' namespace, which is also the policy namespace.
	owner := workloadauthtesting.NewIdentityWorkload(t, watchAPI, namespace, "identity-owner", namespace)
	other := workloadauthtesting.NewIdentityWorkload(t, watchAPI, namespace, "identity-other", namespace)
	nameIdentity(t, objects, owner.Sandbox, owner.Identity.Name)
	nameIdentity(t, objects, other.Sandbox, other.Identity.Name)
	second := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: "second-owned"},
		Spec:       sandboxapi.SandboxSpec{Template: "test-template", Running: true, Identity: &sandboxapi.IdentityRef{Name: owner.Identity.Name}},
	}
	unnamed := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: "unnamed"},
		Spec:       sandboxapi.SandboxSpec{Template: "test-template", Running: true},
	}
	for _, sandbox := range []*sandboxapi.Sandbox{second, unnamed} {
		if err := objects.Create(ctx, sandbox); err != nil {
			t.Fatal(err)
		}
	}
	dial := serveIdentityEnvironments(t, namespace, objects)
	ownerRPC, otherRPC := dial(owner.URI), dial(other.URI)

	// Reads: the owner sees exactly the Sandboxes naming its Identity, one page at a time.
	want := []string{owner.Sandbox.Name, second.Name}
	if got := listedNames(t, ctx, ownerRPC, 1); !slices.Equal(got, want) {
		t.Fatalf("Identity caller listed %v, want %v", got, want)
	}
	if got := watchedNames(t, ctx, ownerRPC); !slices.Equal(got, want) {
		t.Fatalf("Identity caller watched %v, want %v", got, want)
	}
	if _, err := ownerRPC.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: targetOf(owner.Sandbox)}); err != nil {
		t.Fatalf("Identity caller GetEnvironment of its own Sandbox: %v", err)
	}
	for _, foreign := range []*sandboxapi.Sandbox{other.Sandbox, unnamed} {
		if _, err := ownerRPC.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: targetOf(foreign)}); status.Code(err) != codes.NotFound {
			t.Fatalf("Identity caller read %s, which does not name it: %v", foreign.Name, err)
		}
	}
	// Management stays ServiceAccount-only.
	if _, err := ownerRPC.SuspendEnvironment(ctx, &environmentpb.EnvironmentMutationRequest{Target: targetOf(owner.Sandbox)}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("Identity caller SuspendEnvironment: %v, want Unauthenticated", err)
	}

	// Resume: the owner's idle-suspended Sandbox resumes for it only once allowOwnIdentity opts in.
	sandbox := owner.Sandbox
	if err := objects.Get(ctx, client.ObjectKeyFromObject(sandbox), sandbox); err != nil {
		t.Fatal(err)
	}
	idle := sandboxapi.SuspendedByIdle
	sandbox.Status.OperatingMode, sandbox.Status.StopStage = sandboxapi.OperatingModeSuspended, sandboxapi.StopStageStopped
	sandbox.Status.SuspendedBy, sandbox.Status.SuspendedAtWakeGeneration = &idle, proto.Int64(0)
	if err := objects.Status().Update(ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	resume := &environmentpb.ResumeEnvironmentRequest{
		Target: targetOf(sandbox), ResumeToken: lifecycle.NewResumeToken(targetOf(sandbox), 0),
	}
	resumeDenial := func(rpc environmentpb.EnvironmentServiceClient) environmentlifecyclepb.ResumeEligibilityReason {
		t.Helper()
		_, err := rpc.ResumeEnvironment(ctx, resume)
		if status.Code(err) != codes.PermissionDenied {
			t.Fatalf("ResumeEnvironment: %v, want PermissionDenied", err)
		}
		eligibility, ok := status.Convert(err).Details()[0].(*environmentlifecyclepb.ResumeEligibility)
		if !ok {
			t.Fatalf("ResumeEnvironment denial details = %v", status.Convert(err).Details())
		}
		return eligibility.GetReason()
	}
	if reason := resumeDenial(ownerRPC); reason != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED {
		t.Fatalf("owner resume without allowOwnIdentity denied for %v", reason)
	}
	sandbox.Spec.ResumePolicy = &sandboxapi.ResumePolicy{AllowOwnIdentity: true}
	if err := objects.Update(ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	if reason := resumeDenial(otherRPC); reason != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_CALLER_NOT_EXECUTION_ACCOUNT {
		t.Fatalf("another Identity's resume denied for %v", reason)
	}
	descriptor, err := ownerRPC.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: targetOf(sandbox)})
	if err != nil {
		t.Fatal(err)
	}
	if reason := descriptor.GetActions().GetResumeEligibility().GetReason(); reason != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_OWN_IDENTITY ||
		descriptor.GetActions().GetResumeToken() != resume.GetResumeToken() {
		t.Fatalf("owner's resume eligibility = %v, token %q", descriptor.GetActions().GetResumeEligibility(), descriptor.GetActions().GetResumeToken())
	}
	response, err := ownerRPC.ResumeEnvironment(ctx, resume)
	if err != nil || response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED {
		t.Fatalf("owner ResumeEnvironment = %v, %v", response, err)
	}
	if err := objects.Get(ctx, client.ObjectKeyFromObject(sandbox), sandbox); err != nil {
		t.Fatal(err)
	}
	if sandbox.Spec.WakeGeneration != 1 {
		t.Fatalf("resumed Sandbox wake generation = %d, want 1", sandbox.Spec.WakeGeneration)
	}

	// An Identity recreated under the name is another incarnation: the old credential gets nothing.
	if err := objects.Delete(ctx, owner.Identity); err != nil {
		t.Fatal(err)
	}
	if err := objects.Create(ctx, &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: owner.Identity.Name}, Spec: policyapi.IdentitySpec{Description: "successor"},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := ownerRPC.ListEnvironments(ctx, &environmentpb.ListEnvironmentsRequest{Limit: 10}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("a predecessor Identity's credential listed: %v", err)
	}
	if _, err := ownerRPC.ResumeEnvironment(ctx, resume); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("a predecessor Identity's credential resumed: %v", err)
	}
}
