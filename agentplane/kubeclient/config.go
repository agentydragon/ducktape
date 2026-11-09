// Package kubeclient loads the Kubernetes client configuration Agentplane services start with.
package kubeclient

import (
	"github.com/agentydragon/ducktape/agentplane/observability"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

// Config is the client configuration from the kubeconfig file at path, or the Pod's in-cluster configuration when
// path is empty, with traced API calls. Callers set their own rate limit and content type.
func Config(path string) (*rest.Config, error) {
	var config *rest.Config
	var err error
	if path != "" {
		config, err = clientcmd.BuildConfigFromFlags("", path)
	} else {
		config, err = rest.InClusterConfig()
	}
	if err != nil {
		return nil, err
	}
	config.WrapTransport = observability.WrapHTTPTransport(config.WrapTransport)
	return config, nil
}
