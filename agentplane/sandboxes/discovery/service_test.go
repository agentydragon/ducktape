package discovery

import (
	"context"
	"errors"
	"maps"
	"testing"
	"time"

	discoverypb "github.com/agentydragon/ducktape/agentplane/sandboxes/discovery/discoverypb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
)

const (
	discoveryNamespace = "test-envs"
	foreignNamespace   = "other-envs"
	discoveryAudience  = "environment-discovery"
	sandboxName        = "workspace"
	sandboxUID         = "sandbox-uid-current"
	executionUID       = "service-account-uid-current"
	matchingToken      = "matching-account"
	recreatedToken     = "same-name-recreated-account"
	foreignToken       = "foreign-account"
)

type tokenReviewStub map[string]authenticationv1.UserInfo

func (s tokenReviewStub) Create(_ context.Context, request *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	user, ok := s[request.Spec.Token]
	if !ok {
		return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{Audiences: request.Spec.Audiences}}, nil
	}
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     request.Spec.Audiences,
		User:          user,
	}}, nil
}

func testTokenReviews() tokenReviewStub {
	return tokenReviewStub{
		matchingToken:  {Username: "system:serviceaccount:" + discoveryNamespace + ":" + sandboxName, UID: executionUID},
		recreatedToken: {Username: "system:serviceaccount:" + discoveryNamespace + ":" + sandboxName, UID: "service-account-uid-recreated"},
		foreignToken:   {Username: "system:serviceaccount:" + foreignNamespace + ":" + sandboxName, UID: executionUID},
	}
}

type discoveryRPC struct {
	client discoverypb.EnvironmentDiscoveryClient
	conn   *grpc.ClientConn
}

func serveDiscovery(t *testing.T, objects client.Reader, catalog runtimeconfig.Catalog) discoveryRPC {
	t.Helper()
	service, err := New(discoveryNamespace, objects, catalog, nil)
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := auth.NewResolver(testTokenReviews(), discoveryAudience, []string{discoveryNamespace, foreignNamespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(resolver)
	register := func(server *grpc.Server) {
		discoverypb.RegisterEnvironmentDiscoveryServer(server, service)
	}
	connection := grpcmethods.Serve(t, methods, register)
	return discoveryRPC{client: discoverypb.NewEnvironmentDiscoveryClient(connection), conn: connection}
}

func readerWithObjects(t *testing.T, objects ...client.Object) client.Reader {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	return clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()
}

func sandboxObject(uid types.UID) *sandboxapi.Sandbox {
	return &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: sandboxName, UID: uid},
		Spec:       sandboxapi.SandboxSpec{Template: "runner-vm"},
		Status:     sandboxapi.SandboxStatus{OperatingMode: sandboxapi.OperatingModeRunning},
	}
}

func executionAccountObject(sandboxUID types.UID) *corev1.ServiceAccount {
	controller := true
	return &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: discoveryNamespace,
		Name:      sandboxName,
		UID:       executionUID,
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: sandboxapi.GroupVersion.String(), Kind: "Sandbox", Name: sandboxName,
			UID: sandboxUID, Controller: &controller,
		}},
	}}
}

func baseReader(t *testing.T) client.Reader {
	t.Helper()
	sandbox := sandboxObject(sandboxUID)
	return readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID))
}

func bearing(t *testing.T, token string) context.Context {
	t.Helper()
	return metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer "+token)
}

func TestListAndGetReturnTheExactExecutionAccountIdentity(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	listed, err := rpc.client.ListEnvironments(bearing(t, matchingToken), &discoverypb.ListEnvironmentsRequest{Limit: 10})
	if err != nil {
		t.Fatal(err)
	}
	if len(listed.Items) != 1 {
		t.Fatalf("ListEnvironments returned %d items, want 1: %+v", len(listed.Items), listed)
	}
	item := listed.Items[0]
	if item.Target.GetNamespace() != discoveryNamespace || item.Target.GetName() != sandboxName || item.Target.GetUid() != sandboxUID {
		t.Fatalf("target handle = %+v", item.Target)
	}
	if item.ExecutionAccount.GetNamespace() != discoveryNamespace || item.ExecutionAccount.GetName() != sandboxName || item.ExecutionAccount.GetUid() != executionUID {
		t.Fatalf("execution account = %+v", item.ExecutionAccount)
	}
	got, err := rpc.client.GetEnvironment(bearing(t, matchingToken), &discoverypb.GetEnvironmentRequest{Target: item.Target})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetTarget().GetUid() != sandboxUID || got.GetExecutionAccount().GetUid() != executionUID {
		t.Fatalf("GetEnvironment returned target/account %+v / %+v", got.GetTarget(), got.GetExecutionAccount())
	}
}

func TestRecreatedAndForeignAccountsCannotDiscoverTheSandbox(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	target := &discoverypb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	for _, test := range []struct {
		name  string
		token string
	}{
		{name: "same namespace and name but replaced ServiceAccount UID", token: recreatedToken},
		{name: "foreign namespace", token: foreignToken},
	} {
		t.Run(test.name, func(t *testing.T) {
			listed, err := rpc.client.ListEnvironments(bearing(t, test.token), &discoverypb.ListEnvironmentsRequest{Limit: 10})
			if err != nil {
				t.Fatal(err)
			}
			if len(listed.Items) != 0 {
				t.Fatalf("ListEnvironments returned unauthorized items: %+v", listed.Items)
			}
			if _, err := rpc.client.GetEnvironment(bearing(t, test.token), &discoverypb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.NotFound {
				t.Fatalf("GetEnvironment error = %v, want NotFound", err)
			}
		})
	}
}

func TestGetUsesTheSameNotFoundForMissingAndUnauthorizedTargets(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	for _, test := range []struct {
		name   string
		token  string
		target *discoverypb.TargetHandle
	}{
		{name: "missing name", token: matchingToken, target: &discoverypb.TargetHandle{Namespace: discoveryNamespace, Name: "missing", Uid: "missing-uid"}},
		{name: "wrong ServiceAccount UID", token: recreatedToken, target: &discoverypb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}},
		{name: "foreign caller", token: foreignToken, target: &discoverypb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, err := rpc.client.GetEnvironment(bearing(t, test.token), &discoverypb.GetEnvironmentRequest{Target: test.target})
			if status.Code(err) != codes.NotFound || status.Convert(err).Message() != "environment not found" {
				t.Fatalf("GetEnvironment error = %v, want the fixed NotFound response", err)
			}
		})
	}
}

func TestGetRejectsStaleAndDeletingSandboxIncarnations(t *testing.T) {
	for _, test := range []struct {
		name       string
		sandbox    *sandboxapi.Sandbox
		requested  string
		wantListed int
	}{
		{name: "stale UID", sandbox: sandboxObject(sandboxUID), requested: "old-sandbox-uid", wantListed: 1},
		{name: "deleting Sandbox", sandbox: deletingSandbox(), requested: sandboxUID},
	} {
		t.Run(test.name, func(t *testing.T) {
			reader := readerWithObjects(t, test.sandbox, executionAccountObject(test.sandbox.UID))
			rpc := serveDiscovery(t, reader, runtimeconfig.Catalog{})
			_, err := rpc.client.GetEnvironment(bearing(t, matchingToken), &discoverypb.GetEnvironmentRequest{
				Target: &discoverypb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: test.requested},
			})
			if status.Code(err) != codes.NotFound {
				t.Fatalf("GetEnvironment error = %v, want NotFound", err)
			}
			listed, err := rpc.client.ListEnvironments(bearing(t, matchingToken), &discoverypb.ListEnvironmentsRequest{Limit: 10})
			if err != nil {
				t.Fatal(err)
			}
			if len(listed.Items) != test.wantListed {
				t.Fatalf("ListEnvironments returned %d items, want %d: %+v", len(listed.Items), test.wantListed, listed.Items)
			}
		})
	}
}

func deletingSandbox() *sandboxapi.Sandbox {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Finalizers = []string{"test.agentplane/finalizer"}
	deletionTime := metav1.NewTime(time.Now())
	sandbox.DeletionTimestamp = &deletionTime
	return sandbox
}

func TestDiscoveryRequiresControllerOwnershipProof(t *testing.T) {
	for _, test := range []struct {
		name    string
		account *corev1.ServiceAccount
	}{
		{name: "missing execution account"},
		{name: "missing controller owner reference", account: &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: sandboxName, UID: executionUID}}},
		{name: "owner reference names another Sandbox incarnation", account: executionAccountObject("old-sandbox-uid")},
		{name: "owner reference is not controller-owned", account: accountWithoutControllerOwner()},
	} {
		t.Run(test.name, func(t *testing.T) {
			sandbox := sandboxObject(sandboxUID)
			objects := []client.Object{sandbox}
			if test.account != nil {
				objects = append(objects, test.account)
			}
			rpc := serveDiscovery(t, readerWithObjects(t, objects...), runtimeconfig.Catalog{})
			_, err := rpc.client.GetEnvironment(bearing(t, matchingToken), &discoverypb.GetEnvironmentRequest{
				Target: &discoverypb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
			})
			if status.Code(err) != codes.NotFound {
				t.Fatalf("GetEnvironment error = %v, want NotFound", err)
			}
			listed, err := rpc.client.ListEnvironments(bearing(t, matchingToken), &discoverypb.ListEnvironmentsRequest{Limit: 10})
			if err != nil {
				t.Fatal(err)
			}
			if len(listed.Items) != 0 {
				t.Fatalf("ListEnvironments returned Sandbox without ownership proof: %+v", listed.Items)
			}
		})
	}
}

func accountWithoutControllerOwner() *corev1.ServiceAccount {
	account := executionAccountObject(sandboxUID)
	controller := false
	account.OwnerReferences[0].Controller = &controller
	return account
}

func TestDiscoveryValidatesLimitsPageTokensAndExactHandles(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	for _, limit := range []uint32{0, maxLimit + 1} {
		if _, err := rpc.client.ListEnvironments(bearing(t, matchingToken), &discoverypb.ListEnvironmentsRequest{Limit: limit}); status.Code(err) != codes.InvalidArgument {
			t.Errorf("limit %d error = %v, want InvalidArgument", limit, err)
		}
	}
	if _, err := rpc.client.ListEnvironments(bearing(t, matchingToken), &discoverypb.ListEnvironmentsRequest{Limit: 10, PageToken: "next-page"}); status.Code(err) != codes.InvalidArgument {
		t.Errorf("page token error = %v, want InvalidArgument", err)
	}
	for _, target := range []*discoverypb.TargetHandle{
		nil,
		{Namespace: discoveryNamespace, Name: sandboxName},
		{Namespace: discoveryNamespace, Uid: sandboxUID},
		{Name: sandboxName, Uid: sandboxUID},
	} {
		if _, err := rpc.client.GetEnvironment(bearing(t, matchingToken), &discoverypb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.InvalidArgument {
			t.Errorf("target %+v error = %v, want InvalidArgument", target, err)
		}
	}
}

type failingReader struct {
	client.Reader
	failSandboxRead int
	sandboxReads    int
	failAccountRead bool
}

func (r *failingReader) Get(ctx context.Context, key types.NamespacedName, object client.Object, options ...client.GetOption) error {
	switch object.(type) {
	case *sandboxapi.Sandbox:
		r.sandboxReads++
		if r.failSandboxRead == r.sandboxReads {
			return apierrors.NewInternalError(errors.New("injected Sandbox API failure"))
		}
	case *corev1.ServiceAccount:
		if r.failAccountRead {
			return apierrors.NewInternalError(errors.New("injected ServiceAccount API failure"))
		}
	}
	return r.Reader.Get(ctx, key, object, options...)
}

func TestKubernetesReadFailuresReturnUnavailable(t *testing.T) {
	for _, test := range []struct {
		name   string
		reader func(t *testing.T) client.Reader
	}{
		{
			name: "Sandbox read",
			reader: func(t *testing.T) client.Reader {
				return &failingReader{Reader: baseReader(t), failSandboxRead: 1}
			},
		},
		{
			name: "execution ServiceAccount read",
			reader: func(t *testing.T) client.Reader {
				return &failingReader{Reader: baseReader(t), failAccountRead: true}
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			for _, operation := range []string{"GetEnvironment", "ListEnvironments"} {
				t.Run(operation, func(t *testing.T) {
					rpc := serveDiscovery(t, test.reader(t), runtimeconfig.Catalog{})
					var err error
					switch operation {
					case "GetEnvironment":
						_, err = rpc.client.GetEnvironment(bearing(t, matchingToken), &discoverypb.GetEnvironmentRequest{
							Target: &discoverypb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
						})
					case "ListEnvironments":
						_, err = rpc.client.ListEnvironments(bearing(t, matchingToken), &discoverypb.ListEnvironmentsRequest{Limit: 10})
					}
					if status.Code(err) != codes.Unavailable {
						t.Fatalf("%s error = %v, want Unavailable", operation, err)
					}
				})
			}
		})
	}
}

func TestEveryGeneratedRPCRequiresReviewedServiceAccountAdmission(t *testing.T) {
	methods := func(t *testing.T) auth.Methods {
		t.Helper()
		resolver, err := auth.NewResolver(testTokenReviews(), discoveryAudience, []string{discoveryNamespace, foreignNamespace}, auth.WithVerdictTTL(0))
		if err != nil {
			t.Fatal(err)
		}
		return Methods(resolver)
	}
	register := func(server *grpc.Server) {
		service, err := New(discoveryNamespace, baseReader(t), runtimeconfig.Catalog{}, nil)
		if err != nil {
			t.Fatal(err)
		}
		discoverypb.RegisterEnvironmentDiscoveryServer(server, service)
	}
	configured := methods(t)
	connection := grpcmethods.Serve(t, configured, register)
	for _, method := range grpcmethods.Of(discoverypb.EnvironmentDiscovery_ServiceDesc) {
		t.Run(method.Name, func(t *testing.T) {
			if got := status.Code(method.Call(bearing(t, matchingToken), connection)); got != codes.InvalidArgument {
				t.Errorf("account-only reviewed caller reached %s with code %v, want handler InvalidArgument", method.Name, got)
			}
			if got := status.Code(method.Call(bearing(t, "unreviewed-token"), connection)); got != codes.Unauthenticated {
				t.Errorf("unreviewed caller reached %s with code %v, want Unauthenticated", method.Name, got)
			}
			unlisted := maps.Clone(configured)
			delete(unlisted, method.Name)
			if got := status.Code(method.Call(bearing(t, matchingToken), grpcmethods.Serve(t, unlisted, register))); got != codes.PermissionDenied {
				t.Errorf("unlisted RPC %s with reviewed caller got %v, want PermissionDenied", method.Name, got)
			}
		})
	}
}
