package identity

import (
	"errors"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	corev1 "k8s.io/api/core/v1"
)

const (
	hopAudience       = "test-egress"
	projectedAudience = "https://kubernetes.test.invalid"
	testNamespace     = "test-workloads"
)

var (
	api    *kubeapiserver.Server
	runner = auth.Account{Namespace: testNamespace, Name: "test-runner"}
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) error {
		api = server
		return nil
	}))
}

var unreachable = kubeapiserver.UnreachableReviews{Err: errors.New("test API server unreachable")}

func verifier(t *testing.T, hop, projected auth.TokenReviews) *Verifier {
	t.Helper()
	hopResolver, err := auth.NewResolver(hop, hopAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	projectedResolver, err := auth.NewResolver(projected, projectedAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	return NewVerifier(hopResolver, map[string]*auth.Resolver{projectedAudience: projectedResolver})
}

func pod(t *testing.T, account auth.Account) *corev1.Pod {
	t.Helper()
	pod, err := api.Pod(t.Context(), account)
	if err != nil {
		t.Fatal(err)
	}
	return pod
}

func token(t *testing.T, pod *corev1.Pod, audience string) string {
	t.Helper()
	token, err := api.PodToken(t.Context(), pod, audience)
	if err != nil {
		t.Fatal(err)
	}
	return token
}

func TestIdentifyTellsARejectedTokenFromAnUnansweredReview(t *testing.T) {
	a := pod(t, runner)
	good := token(t, a, hopAudience)

	principal, err := verifier(t, api.TokenReviews(), api.TokenReviews()).Identify(t.Context(), good)
	if err != nil || principal.PodUID != string(a.UID) {
		t.Fatalf("Identify(good) = %+v, %v", principal, err)
	}
	for name, c := range map[string]struct {
		verifier *Verifier
		bearer   string
		want     decision.Reason
	}{
		"unknown":    {verifier(t, api.TokenReviews(), api.TokenReviews()), "test-unknown-token", decision.TokenRejected},
		"unanswered": {verifier(t, unreachable, api.TokenReviews()), good, decision.Unavailable},
	} {
		_, err := c.verifier.Identify(t.Context(), c.bearer)
		var refused *Rejection
		if !errors.As(err, &refused) || refused.Reason != c.want {
			t.Errorf("Identify(%s) = %v, want a rejection with reason %s", name, err, c.want)
		}
	}
}

func TestIdentifyRejectsAServiceAccountOutsideTheServedNamespaces(t *testing.T) {
	foreign := token(t, pod(t, auth.Account{Namespace: "test-elsewhere", Name: "test-runner"}), hopAudience)

	_, err := verifier(t, api.TokenReviews(), api.TokenReviews()).Identify(t.Context(), foreign)
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
	a, b := pod(t, runner), pod(t, runner)
	hop, projectedA, projectedB := token(t, a, hopAudience), token(t, a, projectedAudience), token(t, b, projectedAudience)
	v := verifier(t, api.TokenReviews(), api.TokenReviews())
	principal, err := v.Identify(t.Context(), hop)
	if err != nil {
		t.Fatal(err)
	}

	for name, c := range map[string]struct {
		verifier *Verifier
		offered  map[string]string
	}{
		// A caller that holds another Pod's token for this audience cannot have the proxy spend it,
		// though the hop it arrived on authenticated.
		"another pod's token": {v, map[string]string{projectedAudience: projectedB}},
		// Which audiences may be substituted for is the deployment's to list.
		"unconfigured audience": {v, map[string]string{"https://unlisted.test.invalid": projectedA}},
		// Audiences do not substitute for one another: the hop bearer proves the same Pod and is
		// still refused for a destination that validates a different audience.
		"hop bearer":        {v, map[string]string{projectedAudience: hop}},
		"unanswered review": {verifier(t, api.TokenReviews(), unreachable), map[string]string{projectedAudience: projectedA}},
	} {
		if got := c.verifier.VerifyProjected(t.Context(), c.offered, principal); len(got) != 0 {
			t.Errorf("%s: kept %v", name, got)
		}
	}
	got := v.VerifyProjected(t.Context(), map[string]string{projectedAudience: projectedA}, principal)
	if len(got) != 1 || got[projectedAudience] != projectedA {
		t.Errorf("a token for the presenting Pod: kept %v", got)
	}
}
