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

func TestAccountReadsNamespaceSlashName(t *testing.T) {
	var account Account
	if err := account.UnmarshalText([]byte("agentplane/app")); err != nil || account != (Account{Namespace: "agentplane", Name: "app"}) {
		t.Fatalf("namespace/name rejected: %+v, %v", account, err)
	}
	for _, malformed := range []string{"", "app", "/app", "agentplane/", "agentplane/app/extra"} {
		if err := new(Account).UnmarshalText([]byte(malformed)); err == nil {
			t.Fatalf("%q accepted", malformed)
		}
	}
}

func TestRejectsInvalidWorkload(t *testing.T) {
	for name, mutate := range map[string]func(*authenticationv1.TokenReview){
		"unauthenticated": func(r *authenticationv1.TokenReview) { r.Status.Authenticated = false },
		"wrong audience":  func(r *authenticationv1.TokenReview) { r.Status.Audiences = []string{"gateway"} },
		"user token":      func(r *authenticationv1.TokenReview) { r.Status.User.Username = "operator" },
		"other namespace": func(r *authenticationv1.TokenReview) {
			r.Status.User.Username = "system:serviceaccount:elsewhere:app"
		},
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
	cause := errors.New("api unavailable")
	reviews := reviewFunc(func(context.Context, *authenticationv1.TokenReview) (*authenticationv1.TokenReview, error) {
		return nil, cause
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
	if _, err = resolver.ResolveToken(context.Background(), "token"); !errors.Is(err, ErrUnavailable) || !errors.Is(err, cause) {
		t.Fatalf("a failed review is Unavailable and keeps its cause: %v", err)
	}
}

func TestAResolverNeedsAReviewerAnAudienceAndANamespace(t *testing.T) {
	reviews := reviewFunc(func(context.Context, *authenticationv1.TokenReview) (*authenticationv1.TokenReview, error) {
		return accepted(), nil
	})
	if _, err := NewResolver(reviews, "dispatcher", nil); err == nil {
		t.Fatal("a resolver that serves no namespace was built")
	}
	if _, err := NewResolver(reviews, "", []string{"agentplane-staging"}); err == nil {
		t.Fatal("a resolver with no audience was built")
	}
	if _, err := NewResolver(nil, "dispatcher", []string{"agentplane-staging"}); err == nil {
		t.Fatal("a resolver with no reviewer was built")
	}
}
