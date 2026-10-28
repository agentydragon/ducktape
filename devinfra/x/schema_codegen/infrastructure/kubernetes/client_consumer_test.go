package kubernetes_test

import (
	"testing"

	"github.com/agentydragon/ducktape/devinfra/x/schema_codegen/infrastructure/kubernetes/client/clientset/versioned"
	"k8s.io/client-go/rest"
)

func TestGeneratedClientConstructs(t *testing.T) {
	client, err := versioned.NewForConfig(&rest.Config{Host: "https://example.test"})
	if err != nil {
		t.Fatal(err)
	}
	if client == nil {
		t.Fatal("generated clientset is nil")
	}
}
