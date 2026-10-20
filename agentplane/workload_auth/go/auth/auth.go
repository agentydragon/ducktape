// Package auth authenticates Pod-bound workload tokens for Agentplane services.
// Each API surface constructs its own Resolver with its own audience and namespace policy.
package auth

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"golang.org/x/sync/singleflight"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/cache"
)

const (
	podNameClaim  = "authentication.kubernetes.io/pod-name"
	podUIDClaim   = "authentication.kubernetes.io/pod-uid"
	accountPrefix = "system:serviceaccount:"
)

// DefaultVerdictTTL is how long a Resolver reuses the API server's acceptance of a bearer.
const DefaultVerdictTTL = 30 * time.Second

const (
	// maxVerdicts bounds the acceptances held; the least recently used goes first.
	maxVerdicts = 10000
	// reviewTimeout bounds a TokenReview that every call for one bearer shares, so a caller that
	// gives up does not fail the others.
	reviewTimeout = 30 * time.Second
)

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
// is authenticationv1.TokenReviewInterface; a small interface keeps policy tests deterministic.
type TokenReviews interface {
	Create(context.Context, *authenticationv1.TokenReview, metav1.CreateOptions) (*authenticationv1.TokenReview, error)
}

type clock func() time.Time

func (c clock) Now() time.Time { return c() }

// Resolver reviews each bearer with the API server and reuses its acceptance for the verdict TTL:
// a revoked token, or one whose Pod is gone, still resolves for at most that long. A rejection or a
// failed review is never reused, so the next call asks again.
type Resolver struct {
	reviews    TokenReviews
	audience   string
	namespaces map[string]struct{}
	ttl        time.Duration
	now        func() time.Time

	// accepted holds the identity of each bearer the API server accepted, keyed by the bearer's
	// hash so the cache keeps no credential.
	accepted *cache.LRUExpireCache
	flights  singleflight.Group
}

// Option tunes a Resolver.
type Option func(*Resolver)

// WithVerdictTTL replaces DefaultVerdictTTL. Zero reviews every call.
func WithVerdictTTL(ttl time.Duration) Option { return func(r *Resolver) { r.ttl = ttl } }

// WithClock replaces the wall clock that ages acceptances.
func WithClock(now func() time.Time) Option { return func(r *Resolver) { r.now = now } }

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
	r := &Resolver{reviews: reviews, audience: audience, namespaces: allowed, ttl: DefaultVerdictTTL, now: time.Now}
	for _, option := range options {
		option(r)
	}
	r.accepted = cache.NewLRUExpireCacheWithClock(maxVerdicts, clock(r.now))
	return r, nil
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
	user, err := r.review(ctx, token)
	if err != nil {
		return Principal{}, err
	}
	parts := strings.Split(user.Username, ":")
	if len(parts) != 4 || parts[0]+":"+parts[1]+":" != accountPrefix || parts[2] == "" || parts[3] == "" {
		return Principal{}, ErrInvalidBearer
	}
	if _, ok := r.namespaces[parts[2]]; !ok {
		return Principal{}, ErrInvalidBearer
	}
	podName, okName := oneClaim(user.Extra[podNameClaim])
	podUID, okUID := oneClaim(user.Extra[podUIDClaim])
	if !okName || !okUID || user.UID == "" {
		return Principal{}, ErrInvalidBearer
	}
	return Principal{
		Account:           Account{Namespace: parts[2], Name: parts[3]},
		ServiceAccountUID: user.UID,
		PodName:           podName, PodUID: podUID,
	}, nil
}

// review returns who the API server proved the bearer to be: a held acceptance while it is fresh,
// else the answer of a TokenReview that every concurrent call for the bearer waits on together.
func (r *Resolver) review(ctx context.Context, token string) (authenticationv1.UserInfo, error) {
	key := sha256.Sum256([]byte(token))
	if user, found := r.accepted.Get(key); found {
		return user.(authenticationv1.UserInfo), nil
	}
	lookup := r.flights.DoChan(string(key[:]), func() (any, error) {
		// A call that arrived while the flight before it was finishing finds its acceptance.
		if user, found := r.accepted.Get(key); found {
			return user, nil
		}
		shared, cancel := context.WithTimeout(context.WithoutCancel(ctx), reviewTimeout)
		defer cancel()
		user, err := r.createReview(shared, token)
		if err == nil && r.ttl > 0 {
			r.accepted.Add(key, user, r.ttl)
		}
		return user, err
	})
	select {
	case result := <-lookup:
		if result.Err != nil {
			return authenticationv1.UserInfo{}, result.Err
		}
		return result.Val.(authenticationv1.UserInfo), nil
	case <-ctx.Done():
		return authenticationv1.UserInfo{}, fmt.Errorf("%w: %w", ErrUnavailable, ctx.Err())
	}
}

func (r *Resolver) createReview(ctx context.Context, token string) (authenticationv1.UserInfo, error) {
	review, err := r.reviews.Create(ctx, &authenticationv1.TokenReview{
		Spec: authenticationv1.TokenReviewSpec{Token: token, Audiences: []string{r.audience}},
	}, metav1.CreateOptions{})
	if err != nil {
		return authenticationv1.UserInfo{}, fmt.Errorf("%w: %w", ErrUnavailable, err)
	}
	if review == nil || !review.Status.Authenticated || !slices.Contains(review.Status.Audiences, r.audience) {
		return authenticationv1.UserInfo{}, ErrInvalidBearer
	}
	return review.Status.User, nil
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

func oneClaim(values authenticationv1.ExtraValue) (string, bool) {
	returnValue := ""
	if len(values) != 1 || values[0] == "" {
		return returnValue, false
	}
	return values[0], true
}
