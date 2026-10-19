// Package auth authenticates Pod-bound workload tokens for Agentplane services.
// Each API surface constructs its own Resolver with its own audience and namespace policy.
package auth

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	podNameClaim  = "authentication.kubernetes.io/pod-name"
	podUIDClaim   = "authentication.kubernetes.io/pod-uid"
	accountPrefix = "system:serviceaccount:"
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

// Resolver does not cache TokenReview results: a revoked or replaced Pod token fails on the next call.
type Resolver struct {
	reviews    TokenReviews
	audience   string
	namespaces map[string]struct{}
}

func NewResolver(reviews TokenReviews, audience string, namespaces []string) (*Resolver, error) {
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
	return &Resolver{reviews: reviews, audience: audience, namespaces: allowed}, nil
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
// ServiceAccount UID and singular Pod binding claims. It performs a TokenReview on every call.
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
	review, err := r.reviews.Create(ctx, &authenticationv1.TokenReview{
		Spec: authenticationv1.TokenReviewSpec{Token: token, Audiences: []string{r.audience}},
	}, metav1.CreateOptions{})
	if err != nil {
		return Principal{}, fmt.Errorf("%w: %w", ErrUnavailable, err)
	}
	if review == nil || !review.Status.Authenticated || !slices.Contains(review.Status.Audiences, r.audience) {
		return Principal{}, ErrInvalidBearer
	}
	parts := strings.Split(review.Status.User.Username, ":")
	if len(parts) != 4 || parts[0]+":"+parts[1]+":" != accountPrefix || parts[2] == "" || parts[3] == "" {
		return Principal{}, ErrInvalidBearer
	}
	if _, ok := r.namespaces[parts[2]]; !ok {
		return Principal{}, ErrInvalidBearer
	}
	extra := review.Status.User.Extra
	podName, okName := oneClaim(extra[podNameClaim])
	podUID, okUID := oneClaim(extra[podUIDClaim])
	if !okName || !okUID || review.Status.User.UID == "" {
		return Principal{}, ErrInvalidBearer
	}
	return Principal{
		Account:           Account{Namespace: parts[2], Name: parts[3]},
		ServiceAccountUID: review.Status.User.UID,
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

func oneClaim(values authenticationv1.ExtraValue) (string, bool) {
	returnValue := ""
	if len(values) != 1 || values[0] == "" {
		return returnValue, false
	}
	return values[0], true
}
