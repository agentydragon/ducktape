package auth_test

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
)

const testNamespace = "test-callers"

var api *kubeapiserver.Server

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) error {
		api = server
		return nil
	}))
}

func TestBearerTokenUsesOneRFC6750Value(t *testing.T) {
	for _, test := range []struct {
		values []string
		want   string
		valid  bool
	}{
		{values: []string{"Bearer abc.DEF-1_2~3+4/5="}, want: "abc.DEF-1_2~3+4/5=", valid: true},
		{values: []string{"bearer   abc"}, want: "abc", valid: true},
		{values: []string{"Bearer a", "Bearer b"}},
		{values: []string{"Basic abc"}},
		{values: []string{"Bearer\tabc"}},
		{values: []string{"Bearer a b"}},
		{values: []string{"Bearer a, Bearer b"}},
	} {
		got, err := auth.BearerToken(test.values)
		if (err == nil) != test.valid || got != test.want {
			t.Errorf("BearerToken(%q) = %q, %v; want %q, valid=%v", test.values, got, err, test.want, test.valid)
		}
	}
}

func TestAccountReadsNamespaceSlashName(t *testing.T) {
	var account auth.Account
	if err := account.UnmarshalText([]byte("test-namespace/test-app")); err != nil || account != (auth.Account{Namespace: "test-namespace", Name: "test-app"}) {
		t.Fatalf("namespace/name rejected: %+v, %v", account, err)
	}
	for _, malformed := range []string{"", "test-app", "/test-app", "test-namespace/", "test-namespace/test-app/extra"} {
		if err := new(auth.Account).UnmarshalText([]byte(malformed)); err == nil {
			t.Fatalf("%q accepted", malformed)
		}
	}
}
