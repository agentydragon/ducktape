package kubeapiproxy

import (
	"net/http"
	"testing"
)

func TestRequestInfoMatchesKubernetesResourceShapes(t *testing.T) {
	tests := []struct {
		method      string
		path        string
		verb        string
		group       string
		namespace   string
		resource    string
		subresource string
		name        string
	}{
		{http.MethodGet, "/apis/apps/v1/namespaces/prod/deployments/web/scale", "get", "apps", "prod", "deployments", "scale", "web"},
		{http.MethodPost, "/api/v1/namespaces/prod/configmaps", "create", "", "prod", "configmaps", "", ""},
		{http.MethodDelete, "/api/v1/namespaces/prod/pods", "deletecollection", "", "prod", "pods", "", ""},
		{http.MethodGet, "/api/v1/watch/namespaces/prod/pods", "watch", "", "prod", "pods", "", ""},
		{http.MethodGet, "/apis/apps/v1/deployments", "list", "apps", "", "deployments", "", ""},
		{http.MethodPatch, "/api/v1/namespaces/prod/pods/web/status", "patch", "", "prod", "pods", "status", "web"},
		{http.MethodHead, "/api/v1/nodes/worker-1", "get", "", "", "nodes", "", "worker-1"},
	}

	resolver := newRequestInfoResolver()
	for _, test := range tests {
		request, err := http.NewRequest(test.method, "https://proxy.test"+test.path, nil)
		if err != nil {
			t.Fatal(err)
		}
		got, err := resolver.NewRequestInfo(request)
		if err != nil {
			t.Fatalf("%s %s: %v", test.method, test.path, err)
		}
		if !got.IsResourceRequest || got.Verb != test.verb || got.APIGroup != test.group || got.Namespace != test.namespace || got.Resource != test.resource || got.Subresource != test.subresource || got.Name != test.name {
			t.Errorf("%s %s: got %#v", test.method, test.path, got)
		}
	}
}

func TestRequestInfoTreatsDiscoveryAsNonResource(t *testing.T) {
	resolver := newRequestInfoResolver()
	for _, path := range []string{"/", "/api", "/apis", "/apis/apps/v1", "/version", "/openapi/v3"} {
		request, _ := http.NewRequest(http.MethodGet, "https://proxy.test"+path, nil)
		got, err := resolver.NewRequestInfo(request)
		if err != nil {
			t.Fatal(err)
		}
		if got.IsResourceRequest || got.Verb != "get" || got.Path != path {
			t.Errorf("%s: got %#v", path, got)
		}
		rule := requiredRule(attributesFrom(got))
		if len(rule.NonResourceURLs) != 1 || rule.NonResourceURLs[0] != path {
			t.Errorf("%s: rule %#v", path, rule)
		}
	}
}

// The verb decides both the rule sent for authorization and whether the request
// gets stream lifetime enforcement, so it must be exactly kube-apiserver's own.
func TestWatchVerbFollowsKubernetesRequestClassification(t *testing.T) {
	for _, test := range []struct {
		name string
		path string
		verb string
	}{
		{name: "watch true", path: "/api/v1/namespaces/demo/pods?watch=true", verb: "watch"},
		{name: "watch absent", path: "/api/v1/namespaces/demo/pods", verb: "list"},
		{name: "watch false", path: "/api/v1/namespaces/demo/pods?watch=false", verb: "list"},
		{name: "watch 0", path: "/api/v1/namespaces/demo/pods?watch=0", verb: "list"},
		// An unparseable value is true, matching apimachinery's conversion.
		{name: "watch unparseable", path: "/api/v1/namespaces/demo/pods?watch=not-a-boolean", verb: "watch"},
		// A repeated parameter is decided by its first value alone.
		{name: "watch repeated false first", path: "/api/v1/namespaces/demo/pods?watch=false&watch=true", verb: "list"},
		{name: "deprecated path prefix", path: "/api/v1/watch/namespaces/demo/pods", verb: "watch"},
		// kube-apiserver serves a named object through its bounded get handler,
		// so the query parameter does not make one a watch.
		{name: "named object", path: "/api/v1/namespaces/demo/pods/web?watch=true", verb: "get"},
	} {
		t.Run(test.name, func(t *testing.T) {
			resolver := newRequestInfoResolver()
			req, err := http.NewRequest(http.MethodGet, "https://kubernetes"+test.path, nil)
			if err != nil {
				t.Fatal(err)
			}
			info, err := resolver.NewRequestInfo(req)
			if err != nil {
				t.Fatal(err)
			}
			attributes := attributesFrom(info)
			if attributes.Verb != test.verb {
				t.Fatalf("verb = %q, want %q", attributes.Verb, test.verb)
			}
			if got := isStreamingRequest(attributes, req); got != (test.verb == "watch") {
				t.Errorf("isStreamingRequest = %t, want %t", got, test.verb == "watch")
			}
		})
	}
}
