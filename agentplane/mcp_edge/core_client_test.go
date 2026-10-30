package main

import (
	"context"
	"net"
	"net/http"
	"slices"
	"sync/atomic"
	"testing"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	uniqueauth "github.com/modelcontextprotocol/go-sdk/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	kubernetesfake "k8s.io/client-go/kubernetes/fake"
	kubetesting "k8s.io/client-go/testing"
)

var (
	testAccount    = auth.Account{Namespace: "test-callers", Name: "test-caller"}
	reboundAccount = auth.Account{Namespace: "test-callers", Name: "other-test-caller"}
)

func testCallerIndex(admitted bool) *callerIndex {
	health := watchhealth.New()
	health.MarkSynced()
	accounts := sets.New[auth.Account]()
	if admitted {
		accounts.Insert(testAccount)
	}
	return &callerIndex{health: health, mintable: sets.New(testAccount), accounts: accounts}
}

func testServiceAccount(uid string) *corev1.ServiceAccount {
	return serviceAccount(testAccount, uid)
}

func serviceAccount(account auth.Account, uid string) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: account.Namespace, Name: account.Name, UID: types.UID(uid),
		Labels: map[string]string{callerLabel: "true"},
	}}
}

func tokenKube(t *testing.T) (*kubernetesfake.Clientset, <-chan *authenticationv1.TokenRequest) {
	t.Helper()
	kube := kubernetesfake.NewSimpleClientset(testServiceAccount("uid-1"))
	requests := make(chan *authenticationv1.TokenRequest, 2)
	kube.PrependReactor("create", "serviceaccounts", func(action kubetesting.Action) (bool, runtime.Object, error) {
		if action.GetSubresource() != "token" {
			return false, nil, nil
		}
		request := action.(kubetesting.CreateAction).GetObject().(*authenticationv1.TokenRequest)
		requests <- request.DeepCopy()
		return true, &authenticationv1.TokenRequest{Status: authenticationv1.TokenRequestStatus{Token: "minted"}}, nil
	})
	return kube, requests
}

func testCoreCaller(t *testing.T, admitted bool) (*coreCaller, *kubernetesfake.Clientset, <-chan *authenticationv1.TokenRequest) {
	t.Helper()
	kube, requests := tokenKube(t)
	caller := &coreCaller{
		authority: func(_ context.Context, bearer string, _ *http.Request) (*uniqueauth.TokenInfo, error) {
			if bearer != "grant" {
				return nil, uniqueauth.ErrInvalidToken
			}
			return &uniqueauth.TokenInfo{UserID: testAccount.Namespace + "/" + testAccount.Name}, nil
		},
		callers: testCallerIndex(admitted), kube: kube,
	}
	return caller, kube, requests
}

func TestTokenRequestScopeAndUIDReplacement(t *testing.T) {
	ctx := context.WithValue(t.Context(), edgeBearerKey{}, "grant")
	caller, kube, requests := testCoreCaller(t, false)
	if _, _, err := caller.token(ctx); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("unlisted account admitted: %v", err)
	}
	if len(requests) != 0 {
		t.Fatal("minted a token for an unlisted account")
	}
	caller.callers = testCallerIndex(true)
	admitted, identity, err := caller.token(metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer forwarded"))
	if err != nil {
		t.Fatal(err)
	}
	if identity.account != testAccount || identity.uid != "uid-1" {
		t.Fatalf("identity: %+v", identity)
	}
	request := <-requests
	// The live Actions TokenReview accepts this audience; compare with the contract, not the code's constant.
	if !slices.Equal(request.Spec.Audiences, []string{"agentplane-egress"}) || request.Spec.ExpirationSeconds == nil || *request.Spec.ExpirationSeconds != 600 {
		t.Fatalf("TokenRequest scope: %+v", request.Spec)
	}
	if md, _ := metadata.FromOutgoingContext(admitted); !slices.Equal(md.Get("authorization"), []string{"Bearer minted"}) {
		t.Fatalf("forwarded Authorization survived: %v", md.Get("authorization"))
	}
	kube.PrependReactor("create", "serviceaccounts", func(action kubetesting.Action) (bool, runtime.Object, error) {
		if action.GetSubresource() != "token" {
			return false, nil, nil
		}
		if err := kube.Tracker().Update(corev1.SchemeGroupVersion.WithResource("serviceaccounts"), testServiceAccount("uid-2"), testAccount.Namespace); err != nil {
			return true, nil, err
		}
		return true, &authenticationv1.TokenRequest{Status: authenticationv1.TokenRequestStatus{Token: "minted-new"}}, nil
	})
	if _, _, err := caller.token(ctx); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("accepted token across account replacement: %v", err)
	}
}

type watchCore struct {
	pb.UnimplementedActionServiceServer
	started chan<- []string
	release <-chan struct{}
}

func (s *watchCore) WatchActionRequest(_ *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
	md, _ := metadata.FromIncomingContext(stream.Context())
	s.started <- md.Get("authorization")
	if err := stream.Send(&pb.WatchActionRequestResponse{}); err != nil {
		return err
	}
	select {
	case <-s.release:
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
	return stream.Send(&pb.WatchActionRequestResponse{})
}

func TestGeneratedWatchReceivesAfterHalfCloseAndRechecksBeforeEachFrame(t *testing.T) {
	for _, changed := range []string{"grant", "binding", "label", "uid"} {
		t.Run(changed, func(t *testing.T) {
			caller, kube, _ := testCoreCaller(t, true)
			var revoked atomic.Bool
			var rebound atomic.Bool
			if err := kube.Tracker().Add(serviceAccount(reboundAccount, "uid-other")); err != nil {
				t.Fatal(err)
			}
			caller.callers.mu.Lock()
			caller.callers.mintable.Insert(reboundAccount)
			caller.callers.accounts.Insert(reboundAccount)
			caller.callers.mu.Unlock()
			valid := caller.authority
			caller.authority = func(ctx context.Context, bearer string, request *http.Request) (*uniqueauth.TokenInfo, error) {
				if revoked.Load() {
					return nil, uniqueauth.ErrInvalidToken
				}
				if rebound.Load() {
					return &uniqueauth.TokenInfo{UserID: reboundAccount.Namespace + "/" + reboundAccount.Name}, nil
				}
				return valid(ctx, bearer, request)
			}
			listener := bufconn.Listen(1 << 20)
			started := make(chan []string, 1)
			release := make(chan struct{})
			server := grpc.NewServer()
			pb.RegisterActionServiceServer(server, &watchCore{started: started, release: release})
			go func() { _ = server.Serve(listener) }()
			t.Cleanup(server.Stop)
			connection, err := grpc.NewClient("passthrough:///actions", grpc.WithTransportCredentials(insecure.NewCredentials()),
				grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
				grpc.WithStreamInterceptor(caller.stream))
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = connection.Close() })
			ctx, cancel := context.WithTimeout(context.WithValue(t.Context(), edgeBearerKey{}, "grant"), 5*time.Second)
			defer cancel()
			watch, err := pb.NewActionServiceClient(connection).WatchActionRequest(ctx, &pb.WatchActionRequestRequest{})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := watch.Recv(); err != nil {
				t.Fatalf("first frame after generated half-close: %v", err)
			}
			if credentials := <-started; !slices.Equal(credentials, []string{"Bearer minted"}) {
				t.Fatalf("Actions received credentials %q", credentials)
			}
			switch changed {
			case "grant":
				revoked.Store(true)
			case "binding":
				rebound.Store(true)
			case "label":
				unlabeled := testServiceAccount("uid-1")
				delete(unlabeled.Labels, callerLabel)
				if err := kube.Tracker().Update(corev1.SchemeGroupVersion.WithResource("serviceaccounts"), unlabeled, testAccount.Namespace); err != nil {
					t.Fatal(err)
				}
			case "uid":
				if err := kube.Tracker().Update(corev1.SchemeGroupVersion.WithResource("serviceaccounts"), testServiceAccount("uid-2"), testAccount.Namespace); err != nil {
					t.Fatal(err)
				}
			}
			close(release)
			if _, err := watch.Recv(); status.Code(err) != codes.Unauthenticated {
				t.Fatalf("%s change delivered another frame: %v", changed, err)
			}
		})
	}
}
