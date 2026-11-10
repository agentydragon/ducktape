package kubeapiserver

import (
	"context"
	"sync/atomic"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// CountedReviews passes reviews to TokenReviews and counts them.
type CountedReviews struct {
	auth.TokenReviews
	Calls atomic.Int32
}

func (c *CountedReviews) Create(ctx context.Context, review *authenticationv1.TokenReview, options metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	c.Calls.Add(1)
	return c.TokenReviews.Create(ctx, review, options)
}

// UnreachableReviews is an API server that cannot be reached: every review fails in transport with
// Err, which a running API server cannot be made to do.
type UnreachableReviews struct{ Err error }

func (u UnreachableReviews) Create(context.Context, *authenticationv1.TokenReview, metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return nil, u.Err
}
