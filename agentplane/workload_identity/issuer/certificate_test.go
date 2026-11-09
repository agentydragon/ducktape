package issuer

import (
	"testing"

	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
)

// cert-manager's webhook defaults fields of a Certificate the issuer wrote; anything that could give
// its Secret another identity or key policy is a mismatch.
func TestCertificateSpecMatchesCertManagerDefaults(t *testing.T) {
	record := &workloadapi.WorkloadCredential{ObjectMeta: metav1.ObjectMeta{Namespace: "test-workloads", Name: "test-agent", UID: "record-uid"}}
	const uri = "spiffe://agentplane/test-workloads/test-agent/0123456789abcdef0123456789abcdef"
	expected, _, err := unstructured.NestedMap(certificateObject(record, "test-workload-issuer", "svid-test", uri, "0123456789abcdef0123456789abcdef").Object, "spec")
	if err != nil {
		t.Fatal(err)
	}
	withDefaults := runtime.DeepCopyJSON(expected)
	withDefaults["duration"] = "24h0m0s"
	withDefaults["renewBefore"] = "8h0m0s"
	withDefaults["isCA"] = false
	withDefaults["revisionHistoryLimit"] = int64(1)
	withDefaults["privateKey"].(map[string]interface{})["encoding"] = "PKCS8"
	if !certificateSpecMatches(withDefaults, expected) {
		t.Fatal("cert-manager defaults or canonical durations rejected an otherwise matching certificate")
	}

	for name, mutate := range map[string]func(map[string]interface{}){
		"URI": func(spec map[string]interface{}) {
			spec["uris"] = []interface{}{"spiffe://agentplane/test-other/test-agent/0123456789abcdef0123456789abcdef"}
		},
		"issuer": func(spec map[string]interface{}) {
			spec["issuerRef"].(map[string]interface{})["name"] = "test-other-issuer"
		},
		"issuer kind": func(spec map[string]interface{}) { spec["issuerRef"].(map[string]interface{})["kind"] = "Issuer" },
		"secret name": func(spec map[string]interface{}) { spec["secretName"] = "test-shared-secret" },
		"extra usage": func(spec map[string]interface{}) {
			spec["usages"] = []interface{}{"digital signature", "server auth", "code signing"}
		},
		"rotation policy": func(spec map[string]interface{}) {
			spec["privateKey"].(map[string]interface{})["rotationPolicy"] = "Never"
		},
		"extra SAN field": func(spec map[string]interface{}) { spec["dnsNames"] = []interface{}{"other.example"} },
		"a CA":            func(spec map[string]interface{}) { spec["isCA"] = true },
		"secret labels":   func(spec map[string]interface{}) { spec["secretTemplate"] = map[string]interface{}{} },
	} {
		t.Run(name, func(t *testing.T) {
			actual := runtime.DeepCopyJSON(expected)
			mutate(actual)
			if certificateSpecMatches(actual, expected) {
				t.Fatal("certificate with a different identity or key policy was accepted")
			}
		})
	}
}
