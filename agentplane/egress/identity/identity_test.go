package identity

import (
	"context"
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	hopAudience       = "egress"
	projectedAudience = "https://kubernetes.test.invalid"
)

// reviews answers TokenReviews from a table of token to the Pod it is bound to; an absent token is
// not authenticated and a token mapped to "error" fails the call.
type reviews struct {
	audience string
	pods     map[string]string
	// foreign names the tokens of a ServiceAccount outside the namespaces the resolver serves.
	foreign map[string]bool
}

func (r reviews) Create(_ context.Context, review *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	pod, known := r.pods[review.Spec.Token]
	if pod == "error" {
		return nil, errors.New("API server unreachable")
	}
	namespace := "workloads"
	if r.foreign[review.Spec.Token] {
		namespace = "elsewhere"
	}
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: known,
		Audiences:     []string{r.audience},
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:" + namespace + ":runner", UID: "sa-1",
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {pod}, "authentication.kubernetes.io/pod-uid": {"uid-" + pod},
			},
		},
	}}, nil
}

func verifier(t *testing.T, hop, projected map[string]string) *Verifier {
	t.Helper()
	hopResolver, err := auth.NewResolver(reviews{audience: hopAudience, pods: hop, foreign: map[string]bool{"foreign": true}}, hopAudience, []string{"workloads"})
	if err != nil {
		t.Fatal(err)
	}
	projectedResolver, err := auth.NewResolver(reviews{audience: projectedAudience, pods: projected}, projectedAudience, []string{"workloads"})
	if err != nil {
		t.Fatal(err)
	}
	return NewVerifier(hopResolver, map[string]*auth.Resolver{projectedAudience: projectedResolver})
}

func TestIdentifyTellsARejectedTokenFromAnUnansweredReview(t *testing.T) {
	v := verifier(t, map[string]string{"good": "pod-a", "broken": "error"}, nil)

	principal, err := v.Identify(t.Context(), "good")
	if err != nil || principal.PodUID != "uid-pod-a" {
		t.Fatalf("Identify(good) = %+v, %v", principal, err)
	}
	for token, want := range map[string]decision.Reason{"unknown": decision.TokenRejected, "broken": decision.Unavailable} {
		_, err := v.Identify(t.Context(), token)
		var refused *Rejection
		if !errors.As(err, &refused) || refused.Reason != want {
			t.Errorf("Identify(%s) = %v, want a rejection with reason %s", token, err, want)
		}
	}
}

func TestIdentifyRejectsAServiceAccountOutsideTheServedNamespaces(t *testing.T) {
	v := verifier(t, map[string]string{"foreign": "pod-a"}, nil)

	_, err := v.Identify(t.Context(), "foreign")
	var refused *Rejection
	if !errors.As(err, &refused) || refused.Reason != decision.TokenRejected {
		t.Fatalf("Identify(foreign) = %v, want a %s rejection", err, decision.TokenRejected)
	}
}

func TestBearerToken(t *testing.T) {
	for _, c := range []struct {
		values []string
		want   string
		ok     bool
	}{
		{[]string{"Bearer abc.DEF-1_2~3+4/5="}, "abc.DEF-1_2~3+4/5=", true},
		{[]string{"bearer   abc"}, "abc", true},
		{nil, "", false},
		{[]string{"Bearer a", "Bearer b"}, "", false},
		{[]string{"Basic abc"}, "", false},
		{[]string{"Bearer "}, "", false},
		{[]string{"Bearer a b"}, "", false},
		{[]string{"Bearer a, Bearer b"}, "", false},
		{[]string{"Bearer a\r\nX: y"}, "", false},
	} {
		if got, ok := BearerToken(c.values); got != c.want || ok != c.ok {
			t.Errorf("BearerToken(%q) = %q, %v; want %q, %v", c.values, got, ok, c.want, c.ok)
		}
	}
}

func TestParseProjectedTokensKeepsOnlyAudienceTokenPairs(t *testing.T) {
	got := ParseProjectedTokens([]string{"aud-a token-a", "aud-b", " no-audience", "aud-c ", "aud-d token-d"})

	if len(got) != 2 || got["aud-a"] != "token-a" || got["aud-d"] != "token-d" {
		t.Errorf("ParseProjectedTokens = %v", got)
	}
}

func TestVerifyProjectedKeepsOnlyTokensProvenForThePresentingPod(t *testing.T) {
	v := verifier(t, map[string]string{"hop": "pod-a"}, map[string]string{"projected-a": "pod-a", "projected-b": "pod-b", "broken": "error"})
	principal, err := v.Identify(t.Context(), "hop")
	if err != nil {
		t.Fatal(err)
	}

	for name, offered := range map[string]map[string]string{
		// A caller that holds another Pod's token for this audience cannot have the proxy spend it,
		// though the hop it arrived on authenticated.
		"another pod's token": {projectedAudience: "projected-b"},
		// Which audiences may be substituted for is the deployment's to list.
		"unconfigured audience": {"https://unlisted.test.invalid": "projected-a"},
		// Audiences do not substitute for one another: the hop bearer proves the same Pod and is
		// still refused for a destination that validates a different audience.
		"hop bearer":        {projectedAudience: "hop"},
		"unanswered review": {projectedAudience: "broken"},
	} {
		if got := v.VerifyProjected(t.Context(), offered, principal); len(got) != 0 {
			t.Errorf("%s: kept %v", name, got)
		}
	}
	got := v.VerifyProjected(t.Context(), map[string]string{projectedAudience: "projected-a"}, principal)
	if len(got) != 1 || got[projectedAudience] != "projected-a" {
		t.Errorf("a token for the presenting Pod: kept %v", got)
	}
}
