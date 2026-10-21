// Package auth authenticates Pod-bound workload tokens for Agentplane services.
// Each API surface constructs its own Resolver with its own audience and namespace policy.
package auth

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apiserver/pkg/authentication/authenticator"
	"k8s.io/apiserver/pkg/authentication/token/cache"
	"k8s.io/apiserver/pkg/authentication/user"
)

const (
	podNameClaim  = "authentication.kubernetes.io/pod-name"
	podUIDClaim   = "authentication.kubernetes.io/pod-uid"
	accountPrefix = "system:serviceaccount:"
)

// DefaultVerdictTTL is how long a Resolver reuses the API server's acceptance of a bearer.
const DefaultVerdictTTL = 30 * time.Second

var (
	ErrInvalidBearer = errors.New("invalid workload bearer")
	ErrUnavailable   = errors.New("workload authentication unavailable")
)

// Account is the ServiceAccount identity used by caller and method allowlists.
type Account struct {
	Namespace string
	Name      string
}

// UnmarshalText reads the `namespace/name` form deployment settings write an Account in.
func (a *Account) UnmarshalText(text []byte) error {
	namespace, name, found := strings.Cut(string(text), "/")
	if !found || namespace == "" || name == "" || strings.Contains(name, "/") {
		return errors.New("service account must be namespace/name")
	}
	*a = Account{Namespace: namespace, Name: name}
	return nil
}

// Principal is the Pod-bound ServiceAccount identity proven by one TokenReview.
type Principal struct {
	Account           Account
	ServiceAccountUID string
	PodName           string
	PodUID            string
}

// TokenReviews is the Kubernetes TokenReview Create operation. The production implementation
// is authenticationv1.TokenReviewInterface; a small interface lets tests inject the faults a real API
// server cannot be made to produce.
type TokenReviews interface {
	Create(context.Context, *authenticationv1.TokenReview, metav1.CreateOptions) (*authenticationv1.TokenReview, error)
}

// Resolver reviews each bearer with the API server and reuses its acceptance for the verdict TTL
// (the Kubernetes API server's own cached token authenticator): a revoked token, or one whose Pod
// is gone, still resolves for at most that long. A rejection or a failed review is never reused, so
// the next call asks again.
type Resolver struct {
	audience   string
	namespaces map[string]struct{}
	verdicts   authenticator.Token
}

// Option tunes a Resolver.
type Option func(*settings)

type settings struct{ verdictTTL time.Duration }

// WithVerdictTTL replaces DefaultVerdictTTL. Zero reviews every call.
func WithVerdictTTL(ttl time.Duration) Option { return func(s *settings) { s.verdictTTL = ttl } }

func NewResolver(reviews TokenReviews, audience string, namespaces []string, options ...Option) (*Resolver, error) {
	if reviews == nil || audience == "" || len(namespaces) == 0 {
		return nil, fmt.Errorf("token reviewer, audience and allowed namespaces are required")
	}
	allowed := make(map[string]struct{}, len(namespaces))
	for _, namespace := range namespaces {
		if namespace == "" {
			return nil, fmt.Errorf("allowed namespace must not be empty")
		}
		allowed[namespace] = struct{}{}
	}
	configured := settings{verdictTTL: DefaultVerdictTTL}
	for _, option := range options {
		option(&configured)
	}
	return &Resolver{
		audience: audience, namespaces: allowed,
		// Errors and rejections are not cached: failureTTL is zero.
		verdicts: cache.New(tokenReviewer{reviews: reviews, audience: audience}, false, configured.verdictTTL, 0),
	}, nil
}

// tokenReviewer is the uncached authenticator.Token the verdict cache wraps: one TokenReview per call.
type tokenReviewer struct {
	reviews  TokenReviews
	audience string
}

func (t tokenReviewer) AuthenticateToken(ctx context.Context, token string) (*authenticator.Response, bool, error) {
	review, err := t.reviews.Create(ctx, &authenticationv1.TokenReview{
		Spec: authenticationv1.TokenReviewSpec{Token: token, Audiences: []string{t.audience}},
	}, metav1.CreateOptions{})
	if err != nil {
		return nil, false, err
	}
	if review == nil || !review.Status.Authenticated || !slices.Contains(review.Status.Audiences, t.audience) {
		return nil, false, nil
	}
	info := review.Status.User
	extra := make(map[string][]string, len(info.Extra))
	for claim, values := range info.Extra {
		extra[claim] = values
	}
	return &authenticator.Response{
		Audiences: review.Status.Audiences,
		User:      &user.DefaultInfo{Name: info.Username, UID: info.UID, Groups: info.Groups, Extra: extra},
	}, true, nil
}

// Resolve adapts gRPC metadata to ResolveAuthorization. HTTP callers use the same
// authorization parser and TokenReview policy directly.
func (r *Resolver) Resolve(ctx context.Context) (Principal, error) {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return Principal{}, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	principal, err := r.ResolveAuthorization(ctx, md.Get("authorization"))
	switch {
	case errors.Is(err, ErrInvalidBearer):
		return Principal{}, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	case err != nil && ctx.Err() != nil:
		return Principal{}, status.FromContextError(ctx.Err()).Err()
	case errors.Is(err, ErrUnavailable):
		return Principal{}, status.Error(codes.Unavailable, ErrUnavailable.Error())
	default:
		return principal, err
	}
}

// ResolveAuthorization accepts exactly one Bearer value and requires the reviewed audience,
// ServiceAccount UID and singular Pod binding claims.
func (r *Resolver) ResolveAuthorization(ctx context.Context, values []string) (Principal, error) {
	if len(values) != 1 {
		return Principal{}, ErrInvalidBearer
	}
	value := values[0]
	if !strings.HasPrefix(value, "Bearer ") || strings.TrimSpace(value[7:]) == "" || strings.ContainsAny(value[7:], " \t\r\n") {
		return Principal{}, ErrInvalidBearer
	}
	return r.ResolveToken(ctx, value[7:])
}

// ResolveToken reviews one bearer token, however the caller received it. It fails with
// Unauthenticated when the token does not prove a workload this Resolver serves and with
// Unavailable when the TokenReview could not be answered.
func (r *Resolver) ResolveToken(ctx context.Context, token string) (Principal, error) {
	response, authenticated, err := r.verdicts.AuthenticateToken(ctx, token)
	if err != nil {
		return Principal{}, fmt.Errorf("%w: %w", ErrUnavailable, err)
	}
	if !authenticated {
		return Principal{}, ErrInvalidBearer
	}
	parts := strings.Split(response.User.GetName(), ":")
	if len(parts) != 4 || parts[0]+":"+parts[1]+":" != accountPrefix || parts[2] == "" || parts[3] == "" {
		return Principal{}, ErrInvalidBearer
	}
	if _, ok := r.namespaces[parts[2]]; !ok {
		return Principal{}, ErrInvalidBearer
	}
	extra := response.User.GetExtra()
	podName, okName := oneClaim(extra[podNameClaim])
	podUID, okUID := oneClaim(extra[podUIDClaim])
	if !okName || !okUID || response.User.GetUID() == "" {
		return Principal{}, ErrInvalidBearer
	}
	return Principal{
		Account:           Account{Namespace: parts[2], Name: parts[3]},
		ServiceAccountUID: response.User.GetUID(),
		PodName:           podName, PodUID: podUID,
	}, nil
}

// RequireAccount applies an API/method-specific allowlist after token validation. It does not
// grant wake or command permission merely because an account can call the Gateway proxy.
func (r *Resolver) RequireAccount(ctx context.Context, allowed map[Account]struct{}) (Principal, error) {
	principal, err := r.Resolve(ctx)
	if err != nil {
		return Principal{}, err
	}
	if _, ok := allowed[principal.Account]; !ok {
		return Principal{}, status.Error(codes.PermissionDenied, "service caller not allowed")
	}
	return principal, nil
}

func oneClaim(values []string) (string, bool) {
	returnValue := ""
	if len(values) != 1 || values[0] == "" {
		return returnValue, false
	}
	return values[0], true
}
