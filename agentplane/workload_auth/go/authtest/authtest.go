// Package authtest fakes the Kubernetes TokenReview API for tests of a service that authenticates workload
// tokens with auth.Resolver.
package authtest

import (
	"context"

	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Account is the ServiceAccount a test token proves.
type Account struct{ Namespace, Name, UID string }

// Reviews is an auth.TokenReviews that authenticates exactly the tokens in Accounts, as a Pod's projected
// ServiceAccount token. Any other token is not authenticated.
type Reviews struct {
	// Audience is the one audience every token proves; empty proves whichever audiences a review asks for.
	Audience string
	// Accounts maps a bearer token to the account it proves.
	Accounts map[string]Account
}

func (r Reviews) Create(_ context.Context, review *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	account, known := r.Accounts[review.Spec.Token]
	audiences := review.Spec.Audiences
	if r.Audience != "" {
		audiences = []string{r.Audience}
	}
	reviewed := &authenticationv1.TokenReview{Spec: review.Spec, Status: authenticationv1.TokenReviewStatus{
		Authenticated: known, Audiences: audiences,
	}}
	if known {
		reviewed.Status.User = authenticationv1.UserInfo{
			Username: "system:serviceaccount:" + account.Namespace + ":" + account.Name,
			UID:      account.UID,
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {"test-pod"},
				"authentication.kubernetes.io/pod-uid":  {"test-pod-uid"},
			},
		}
	}
	return reviewed, nil
}
