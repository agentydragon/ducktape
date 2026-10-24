package main

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	uniqueauth "github.com/modelcontextprotocol/go-sdk/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

const (
	actionAudience     = "agentplane-egress" // The deployed Action Service's current TokenReview audience.
	watchRecheckPeriod = 5 * time.Second
)

type edgeBearerKey struct{}

type callerIdentity struct {
	account auth.Account
	uid     string
}

// coreCaller crosses the edge-to-Actions authority boundary. Each RPC resolves the OAuth bearer again, then mints
// a short-lived token for the current binding. A watch also rechecks its grant, label and UID every five seconds.
type coreCaller struct {
	authority uniqueauth.TokenVerifier
	callers   *callerIndex
	kube      kubernetes.Interface
}

func (c *coreCaller) identity(ctx context.Context) (callerIdentity, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	bearer, _ := ctx.Value(edgeBearerKey{}).(string)
	if bearer == "" {
		return callerIdentity{}, status.Error(codes.Unauthenticated, "invalid bearer")
	}
	current, err := c.authority(ctx, bearer, nil)
	switch {
	case errors.Is(err, uniqueauth.ErrInvalidToken):
		return callerIdentity{}, status.Error(codes.Unauthenticated, "invalid bearer")
	case err != nil:
		return callerIdentity{}, status.Error(codes.Unavailable, "Connection authority unavailable")
	}
	namespace, name, ok := strings.Cut(current.UserID, "/")
	if !ok || namespace == "" || name == "" {
		return callerIdentity{}, status.Error(codes.Internal, "invalid Connection binding")
	}
	account := auth.Account{Namespace: namespace, Name: name}
	if !c.callers.Admits(account) {
		return callerIdentity{}, status.Error(codes.Unauthenticated, "invalid bearer")
	}
	serviceAccount, err := c.kube.CoreV1().ServiceAccounts(namespace).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		if apierrors.IsNotFound(err) {
			return callerIdentity{}, status.Error(codes.Unauthenticated, "invalid bearer")
		}
		slog.WarnContext(ctx, "reading bound ServiceAccount failed", "error", err)
		return callerIdentity{}, status.Error(codes.Unavailable, "caller lookup unavailable")
	}
	if serviceAccount.Labels[callerLabel] != "true" || serviceAccount.UID == "" {
		return callerIdentity{}, status.Error(codes.Unauthenticated, "invalid bearer")
	}
	return callerIdentity{account: account, uid: string(serviceAccount.UID)}, nil
}

func (c *coreCaller) token(ctx context.Context) (context.Context, callerIdentity, error) {
	requestContext := ctx
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	identity, err := c.identity(ctx)
	if err != nil {
		return nil, callerIdentity{}, err
	}
	expiry := int64(600)
	issued, err := c.kube.CoreV1().ServiceAccounts(identity.account.Namespace).CreateToken(ctx, identity.account.Name,
		&authenticationv1.TokenRequest{Spec: authenticationv1.TokenRequestSpec{
			Audiences: []string{actionAudience}, ExpirationSeconds: &expiry,
		}}, metav1.CreateOptions{})
	if err != nil {
		if apierrors.IsNotFound(err) {
			return nil, callerIdentity{}, status.Error(codes.Unauthenticated, "invalid bearer")
		}
		slog.WarnContext(ctx, "minting Action caller token failed", "error", err)
		return nil, callerIdentity{}, status.Error(codes.Unavailable, "caller token unavailable")
	}
	// A replacement between the two reads changes the UID and refuses the token. A replacement after this read
	// makes the token's old UID invalid when Actions TokenReviews it and checks the current caller label.
	current, err := c.identity(ctx)
	if err != nil {
		return nil, callerIdentity{}, err
	}
	if current != identity || issued.Status.Token == "" {
		return nil, callerIdentity{}, status.Error(codes.Unauthenticated, "invalid bearer")
	}
	previous, _ := metadata.FromOutgoingContext(ctx)
	outgoing := previous.Copy()
	outgoing.Set("authorization", "Bearer "+issued.Status.Token)
	// The token request's own deadline ends here; the RPC retains its parent's deadline and cancellation.
	return metadata.NewOutgoingContext(requestContext, outgoing), identity, nil
}

func (c *coreCaller) recheck(ctx context.Context, original callerIdentity) error {
	current, err := c.identity(ctx)
	if err != nil {
		return err
	}
	if current != original {
		return status.Error(codes.Unauthenticated, "invalid bearer")
	}
	return nil
}

func (c *coreCaller) unary(ctx context.Context, method string, request, reply any, connection *grpc.ClientConn, invoke grpc.UnaryInvoker, options ...grpc.CallOption) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	admitted, _, err := c.token(ctx)
	if err != nil {
		return err
	}
	return invoke(admitted, method, request, reply, connection, options...)
}

func (c *coreCaller) stream(ctx context.Context, description *grpc.StreamDesc, connection *grpc.ClientConn, method string, start grpc.Streamer, options ...grpc.CallOption) (grpc.ClientStream, error) {
	admitted, identity, err := c.token(ctx)
	if err != nil {
		return nil, err
	}
	watchCtx, cancel := context.WithCancel(admitted)
	stream, err := start(watchCtx, description, connection, method, options...)
	if err != nil {
		cancel()
		return nil, err
	}
	checked := &checkedStream{ClientStream: stream, caller: c, ctx: watchCtx, cancel: cancel, identity: identity}
	go checked.watch()
	return checked, nil
}

// checkedStream fails an open watch as soon as the Connection or account changes. The Action Service also
// revalidates the minted token before its own frames; both checks are needed because the grants live at this edge.
type checkedStream struct {
	grpc.ClientStream
	caller   *coreCaller
	ctx      context.Context
	cancel   context.CancelFunc
	identity callerIdentity
	mu       sync.Mutex
	err      error
}

func (s *checkedStream) watch() {
	ticker := time.NewTicker(watchRecheckPeriod)
	defer ticker.Stop()
	for {
		select {
		case <-s.ctx.Done():
			return
		case <-ticker.C:
			if err := s.caller.recheck(s.ctx, s.identity); err != nil {
				s.mu.Lock()
				s.err = err
				s.mu.Unlock()
				s.cancel()
				return
			}
		}
	}
}

func (s *checkedStream) RecvMsg(message any) error {
	err := s.ClientStream.RecvMsg(message)
	s.mu.Lock()
	revoked := s.err
	s.mu.Unlock()
	if revoked != nil {
		return revoked
	}
	if err != nil {
		s.cancel()
		return err
	}
	if err := s.caller.recheck(s.ctx, s.identity); err != nil {
		s.cancel()
		return err
	}
	return nil
}

func (s *checkedStream) SendMsg(message any) error {
	err := s.ClientStream.SendMsg(message)
	if err != nil {
		s.cancel()
	}
	return err
}

func (s *checkedStream) CloseSend() error {
	err := s.ClientStream.CloseSend()
	if err != nil {
		s.cancel()
	}
	return err
}

func dialActions(target string, caller *coreCaller) (*grpc.ClientConn, error) {
	return grpc.NewClient(
		target,
		observability.GRPCClientOption(),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithUnaryInterceptor(caller.unary), grpc.WithStreamInterceptor(caller.stream), grpc.WithDisableRetry(),
	)
}
