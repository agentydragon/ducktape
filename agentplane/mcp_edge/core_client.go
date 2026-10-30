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

// The deployed Action Service's current TokenReview audience.
const actionAudience = "agentplane-egress"

type edgeBearerKey struct{}

type callerIdentity struct {
	account auth.Account
	uid     string
}

// coreCaller crosses the edge-to-Actions authority boundary. Each RPC resolves the OAuth bearer again, reads the
// account's UID and caller label from the informer-backed index, and sends a cached token for that UID. Revocation
// latency is therefore informer lag, and an unhealthy index refuses everything. An open stream rechecks on a ticker.
type coreCaller struct {
	authority     uniqueauth.TokenVerifier
	callers       *callerIndex
	kube          kubernetes.Interface
	recheckPeriod time.Duration

	mu     sync.Mutex
	tokens map[auth.Account]cachedToken
}

// cachedToken is valid only for the ServiceAccount UID it was minted for; a recreated account has another UID.
type cachedToken struct {
	uid       string
	token     string
	refreshAt time.Time
}

// refreshFraction is the share of a token's lifetime after which it is replaced, as the kubelet's token manager does.
const refreshFraction = 0.8

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
	uid, ok := c.callers.CurrentUID(account)
	if !ok {
		return callerIdentity{}, status.Error(codes.Unauthenticated, "invalid bearer")
	}
	return callerIdentity{account: account, uid: uid}, nil
}

// serviceAccountToken returns a token for the identity, minting one only when none is cached for this UID or the
// cached one is past its refresh point. The mutex serializes minting so concurrent calls share one TokenRequest.
func (c *coreCaller) serviceAccountToken(ctx context.Context, identity callerIdentity) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if cached, ok := c.tokens[identity.account]; ok && cached.uid == identity.uid && time.Now().Before(cached.refreshAt) {
		return cached.token, nil
	}
	delete(c.tokens, identity.account)
	expiry := int64(600)
	requested := time.Now()
	issued, err := c.kube.CoreV1().ServiceAccounts(identity.account.Namespace).CreateToken(ctx, identity.account.Name,
		&authenticationv1.TokenRequest{Spec: authenticationv1.TokenRequestSpec{
			Audiences: []string{actionAudience}, ExpirationSeconds: &expiry,
		}}, metav1.CreateOptions{})
	if err != nil {
		if apierrors.IsNotFound(err) {
			return "", status.Error(codes.Unauthenticated, "invalid bearer")
		}
		slog.WarnContext(ctx, "minting Action caller token failed", "error", err)
		return "", status.Error(codes.Unavailable, "caller token unavailable")
	}
	if issued.Status.Token == "" {
		return "", status.Error(codes.Unavailable, "caller token unavailable")
	}
	// A replacement between the identity read and the mint changes the UID in the informer and refuses the token. A
	// replacement after this makes the token's old UID invalid when Actions TokenReviews it.
	if uid, ok := c.callers.CurrentUID(identity.account); !ok || uid != identity.uid {
		return "", status.Error(codes.Unauthenticated, "invalid bearer")
	}
	// An absent expiry yields a non-positive lifetime, so the token is used once and never reused.
	lifetime := issued.Status.ExpirationTimestamp.Time.Sub(requested)
	c.tokens[identity.account] = cachedToken{
		uid: identity.uid, token: issued.Status.Token,
		refreshAt: requested.Add(time.Duration(float64(lifetime) * refreshFraction)),
	}
	return issued.Status.Token, nil
}

func (c *coreCaller) token(ctx context.Context) (context.Context, callerIdentity, error) {
	requestContext := ctx
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	identity, err := c.identity(ctx)
	if err != nil {
		return nil, callerIdentity{}, err
	}
	token, err := c.serviceAccountToken(ctx, identity)
	if err != nil {
		return nil, callerIdentity{}, err
	}
	previous, _ := metadata.FromOutgoingContext(ctx)
	outgoing := previous.Copy()
	outgoing.Set("authorization", "Bearer "+token)
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

// checkedStream fails an open watch within one recheck period of the Connection or account changing; frames are not
// rechecked individually. The Action Service also revalidates the minted token before its own frames; both checks are needed because the grants live at this edge.
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
	ticker := time.NewTicker(s.caller.recheckPeriod)
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
