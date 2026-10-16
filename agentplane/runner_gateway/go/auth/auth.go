// Package auth authenticates Pod-bound workload tokens for the unified Runner Gateway.
// Each API surface constructs its own Resolver with its own audience and namespace policy.
package auth

import (
	"context"
	"fmt"
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

// Account is the ServiceAccount identity used by caller and method allowlists.
type Account struct {
	Namespace string
	Name      string
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

// Resolve accepts exactly one Bearer metadata value and requires the reviewed audience,
// ServiceAccount UID and singular Pod binding claims. It performs a TokenReview on every call.
func (r *Resolver) Resolve(ctx context.Context) (Principal, error) {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok || len(md.Get("authorization")) != 1 {
		return Principal{}, status.Error(codes.Unauthenticated, "invalid workload bearer")
	}
	value := md.Get("authorization")[0]
	if !strings.HasPrefix(value, "Bearer ") || strings.TrimSpace(value[7:]) == "" || strings.ContainsAny(value[7:], " \t\r\n") {
		return Principal{}, status.Error(codes.Unauthenticated, "invalid workload bearer")
	}
	review, err := r.reviews.Create(ctx, &authenticationv1.TokenReview{
		Spec: authenticationv1.TokenReviewSpec{Token: value[7:], Audiences: []string{r.audience}},
	}, metav1.CreateOptions{})
	if err != nil {
		return Principal{}, status.Error(codes.Unavailable, "workload authentication unavailable")
	}
	if review == nil || !review.Status.Authenticated || !contains(review.Status.Audiences, r.audience) {
		return Principal{}, status.Error(codes.Unauthenticated, "invalid workload bearer")
	}
	parts := strings.Split(review.Status.User.Username, ":")
	if len(parts) != 4 || parts[0]+":"+parts[1]+":" != accountPrefix || parts[2] == "" || parts[3] == "" {
		return Principal{}, status.Error(codes.Unauthenticated, "invalid workload bearer")
	}
	if _, ok := r.namespaces[parts[2]]; !ok {
		return Principal{}, status.Error(codes.Unauthenticated, "invalid workload bearer")
	}
	extra := review.Status.User.Extra
	podName, okName := oneClaim(extra[podNameClaim])
	podUID, okUID := oneClaim(extra[podUIDClaim])
	if !okName || !okUID || review.Status.User.UID == "" {
		return Principal{}, status.Error(codes.Unauthenticated, "invalid workload bearer")
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

func contains(values []string, value string) bool {
	for _, candidate := range values {
		if candidate == value {
			return true
		}
	}
	return false
}

func oneClaim(values authenticationv1.ExtraValue) (string, bool) {
	returnValue := ""
	if len(values) != 1 || values[0] == "" {
		return returnValue, false
	}
	return values[0], true
}
