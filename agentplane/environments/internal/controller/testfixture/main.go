// render_pod assembles the actual controller Pod from a SandboxTemplate for admission tests.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	sandboxcontroller "github.com/agentydragon/ducktape/agentplane/environments/internal/controller"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/requester"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

func main() {
	namespace := flag.String("namespace", "", "namespace for the synthetic Sandbox")
	flag.Parse()
	if err := writePod(*namespace); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func writePod(namespace string) error {
	const name = "test-sandbox"
	const id = "0123456789abcdef0123456789abcdef"
	var template sandboxapi.SandboxTemplate
	if err := json.NewDecoder(os.Stdin).Decode(&template); err != nil {
		return fmt.Errorf("decode SandboxTemplate: %w", err)
	}
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: name, UID: "019afb11-ab00-7000-8000-000000000000",
	}}
	pod, blocked := sandboxcontroller.RenderPod(sandbox, &template, requester.Issued{ID: id, SecretName: "svid-" + name + "-" + id})
	if blocked != nil {
		return fmt.Errorf("render Pod: %+v", blocked)
	}
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		return err
	}
	if err := controllerutil.SetControllerReference(sandbox, pod, scheme); err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(pod)
}
