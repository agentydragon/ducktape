package auth

import (
	"context"
	"errors"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type reviewFunc func(context.Context, *authenticationv1.TokenReview) (*authenticationv1.TokenReview, error)

func (f reviewFunc) Create(ctx context.Context, review *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return f(ctx, review)
}

func accepted() *authenticationv1.TokenReview {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     []string{"dispatcher"},
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:agentplane-staging:app",
			UID:      "service-account-uid",
			Extra: map[string]authenticationv1.ExtraValue{
				podNameClaim: {"app-pod"},
				podUIDClaim:  {"pod-uid"},
			},
		},
	}}
}

func incoming(values ...string) context.Context {
	return metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", values[0]))
}

func TestResolveAndAuthorize(t *testing.T) {
	calls := 0
	reviews := reviewFunc(func(_ context.Context, review *authenticationv1.TokenReview) (*authenticationv1.TokenReview, error) {
		calls++
		if review.Spec.Token != "token" || len(review.Spec.Audiences) != 1 || review.Spec.Audiences[0] != "dispatcher" {
			t.Errorf("TokenReview lost token or audience: %+v", review.Spec)
		}
		return accepted(), nil
	})
	resolver, err := NewResolver(reviews, "dispatcher", []string{"agentplane-staging"})
	if err != nil {
		t.Fatal(err)
	}
	app := Account{Namespace: "agentplane-staging", Name: "app"}
	principal, err := resolver.RequireAccount(incoming("Bearer token"), map[Account]struct{}{app: {}})
	if err != nil {
		t.Fatal(err)
	}
	if principal.Account != app || principal.PodUID != "pod-uid" || principal.ServiceAccountUID != "service-account-uid" {
		t.Fatalf("wrong principal: %+v", principal)
	}
	_, err = resolver.RequireAccount(incoming("Bearer token"), map[Account]struct{}{{Namespace: "agentplane-staging", Name: "notifications"}: {}})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("wrong account: %v", err)
	}
	if calls != 2 {
		t.Fatalf("TokenReview must run on each call, got %d", calls)
	}
}

func TestRejectsInvalidWorkload(t *testing.T) {
	for name, mutate := range map[string]func(*authenticationv1.TokenReview){
		"unauthenticated":     func(r *authenticationv1.TokenReview) { r.Status.Authenticated = false },
		"wrong audience":      func(r *authenticationv1.TokenReview) { r.Status.Audiences = []string{"gateway"} },
		"user token":          func(r *authenticationv1.TokenReview) { r.Status.User.Username = "operator" },
		"missing pod UID":     func(r *authenticationv1.TokenReview) { delete(r.Status.User.Extra, podUIDClaim) },
		"multiple pod names":  func(r *authenticationv1.TokenReview) { r.Status.User.Extra[podNameClaim] = []string{"one", "two"} },
		"missing account UID": func(r *authenticationv1.TokenReview) { r.Status.User.UID = "" },
	} {
		t.Run(name, func(t *testing.T) {
			resolver, err := NewResolver(reviewFunc(func(context.Context, *authenticationv1.TokenReview) (*authenticationv1.TokenReview, error) {
				review := accepted()
				mutate(review)
				return review, nil
			}), "dispatcher", []string{"agentplane-staging"})
			if err != nil {
				t.Fatal(err)
			}
			_, err = resolver.Resolve(incoming("Bearer token"))
			if status.Code(err) != codes.Unauthenticated {
				t.Fatalf("got %v", err)
			}
		})
	}
}

func TestMetadataAndReviewFailure(t *testing.T) {
	reviews := reviewFunc(func(context.Context, *authenticationv1.TokenReview) (*authenticationv1.TokenReview, error) {
		return nil, errors.New("api unavailable")
	})
	resolver, err := NewResolver(reviews, "dispatcher", []string{"agentplane-staging"})
	if err != nil {
		t.Fatal(err)
	}
	for _, ctx := range []context.Context{
		context.Background(),
		metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "token")),
		metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer a", "authorization", "Bearer b")),
	} {
		_, err := resolver.Resolve(ctx)
		if status.Code(err) != codes.Unauthenticated {
			t.Fatalf("got %v", err)
		}
	}
	_, err = resolver.Resolve(incoming("Bearer token"))
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("got %v", err)
	}
}
