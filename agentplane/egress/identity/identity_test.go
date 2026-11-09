package identity

import (
	"errors"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	corev1 "k8s.io/api/core/v1"
)

const (
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

func verifier(t *testing.T, reviews auth.TokenReviews) *Verifier {
	t.Helper()
	resolver, err := auth.NewResolver(reviews, projectedAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	return NewVerifier(map[string]*auth.Resolver{projectedAudience: resolver})
}

func pod(t *testing.T, account auth.Account) *corev1.Pod {
	t.Helper()
	pod, err := api.Pod(t.Context(), account)
	if err != nil {
		t.Fatal(err)
	}
	return pod
}

func token(t *testing.T, pod *corev1.Pod) string {
	t.Helper()
	return tokenForAudience(t, pod, projectedAudience)
}

func tokenForAudience(t *testing.T, pod *corev1.Pod, audience string) string {
	t.Helper()
	token, err := api.PodToken(t.Context(), pod, audience)
	if err != nil {
		t.Fatal(err)
	}
	return token
}

func TestParseProjectedTokensKeepsOnlyAudienceTokenPairs(t *testing.T) {
	got := ParseProjectedTokens([]string{"aud-a token-a", "aud-b", " no-audience", "aud-c ", "aud-d token-d"})
	if len(got) != 2 || got["aud-a"] != "token-a" || got["aud-d"] != "token-d" {
		t.Errorf("ParseProjectedTokens = %v", got)
	}
}

func TestVerifyProjectedForSandboxRequiresMatchingAccountAndPodUID(t *testing.T) {
	a, b := pod(t, runner), pod(t, runner)
	otherAccount := auth.Account{Namespace: testNamespace, Name: "another-runner"}
	c := pod(t, otherAccount)
	v := verifier(t, api.TokenReviews())
	differentAudience := tokenForAudience(t, a, "https://other.test.invalid")
	for name, c := range map[string]struct {
		token  string
		podUID string
	}{
		"same account but different Pod": {token(t, b), string(a.UID)},
		"different account":              {token(t, c), string(a.UID)},
		"different audience":             {differentAudience, string(a.UID)},
	} {
		t.Run(name, func(t *testing.T) {
			got := v.VerifyProjectedForSandbox(t.Context(), map[string]string{projectedAudience: c.token}, runner, c.podUID)
			if len(got) != 0 {
				t.Fatalf("kept a mismatched token: %v", got)
			}
		})
	}
	valid := token(t, a)
	got := v.VerifyProjectedForSandbox(t.Context(), map[string]string{projectedAudience: valid}, runner, string(a.UID))
	if got[projectedAudience] != valid {
		t.Fatalf("kept token = %v, want matching SVID account and Pod token", got)
	}
}

func TestVerifyProjectedForSandboxDropsUnavailableReviews(t *testing.T) {
	a := pod(t, runner)
	v := verifier(t, unreachable)
	got := v.VerifyProjectedForSandbox(t.Context(), map[string]string{projectedAudience: token(t, a)}, runner, string(a.UID))
	if len(got) != 0 {
		t.Fatalf("kept token while TokenReview was unavailable: %v", got)
	}
}
