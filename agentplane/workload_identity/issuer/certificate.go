package issuer

import (
	"time"

	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"k8s.io/apimachinery/pkg/api/equality"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

const (
	certificateGroup = "cert-manager.io"
	// cert-manager writes it on every Secret it issues.
	certificateNameAnnotation = "cert-manager.io/certificate-name"
)

var (
	certificateGVK     = schema.GroupVersionKind{Group: certificateGroup, Version: "v1", Kind: "Certificate"}
	certificateListGVK = schema.GroupVersionKind{Group: certificateGroup, Version: "v1", Kind: "CertificateList"}
)

// NewCertificate is an empty cert-manager Certificate, for reads and the manager cache.
func NewCertificate() *unstructured.Unstructured {
	certificate := &unstructured.Unstructured{}
	certificate.SetGroupVersionKind(certificateGVK)
	return certificate
}

// certificateObject is the Certificate issuing record's credential id into Secret name. Its labels,
// and through secretTemplate its Secret's, carry the record's UID: cert-manager sets no owner on a
// Secret, so the label is how a superseded generation's Secret is found.
func certificateObject(record *workloadapi.WorkloadCredential, issuer, name, uri, id string) *unstructured.Unstructured {
	owner := metav1.NewControllerRef(record, workloadapi.SchemeGroupVersion.WithKind(workloadapi.Kind))
	labels := func() map[string]interface{} {
		return map[string]interface{}{RecordUIDLabel: string(record.UID), CredentialIDLabel: id}
	}
	return &unstructured.Unstructured{Object: map[string]interface{}{
		"apiVersion": certificateGVK.GroupVersion().String(),
		"kind":       certificateGVK.Kind,
		"metadata": map[string]interface{}{
			"name":      name,
			"namespace": record.Namespace,
			"labels":    labels(),
			"ownerReferences": []interface{}{map[string]interface{}{
				"apiVersion":         owner.APIVersion,
				"kind":               owner.Kind,
				"name":               owner.Name,
				"uid":                string(owner.UID),
				"controller":         true,
				"blockOwnerDeletion": true,
			}},
		},
		"spec": map[string]interface{}{
			"secretName":     name,
			"duration":       "24h",
			"renewBefore":    "8h",
			"issuerRef":      map[string]interface{}{"name": issuer, "kind": "ClusterIssuer", "group": certificateGroup},
			"uris":           []interface{}{uri},
			"usages":         []interface{}{"digital signature", "server auth", "client auth"},
			"privateKey":     map[string]interface{}{"algorithm": "ECDSA", "size": int64(256), "rotationPolicy": "Always"},
			"secretTemplate": map[string]interface{}{"labels": labels()},
		},
	}}
}

// certificateSpecMatches compares the identity and key policy the issuer owns. cert-manager's webhook
// may add harmless defaults such as privateKey.encoding and isCA=false; any additional SAN or usage,
// which could give the Secret another workload's identity, is a mismatch.
func certificateSpecMatches(actual, expected map[string]interface{}) bool {
	allowedFields := map[string]struct{}{
		"secretName": {}, "duration": {}, "renewBefore": {}, "issuerRef": {}, "uris": {},
		"usages": {}, "privateKey": {}, "secretTemplate": {}, "isCA": {}, "revisionHistoryLimit": {},
	}
	for field := range actual {
		if _, ok := allowedFields[field]; !ok {
			return false
		}
	}
	for _, field := range []string{"secretName", "issuerRef", "uris", "usages", "secretTemplate"} {
		if !equality.Semantic.DeepEqual(actual[field], expected[field]) {
			return false
		}
	}
	for _, field := range []string{"duration", "renewBefore"} {
		actualDuration, actualOK := actual[field].(string)
		expectedDuration, expectedOK := expected[field].(string)
		actualValue, actualErr := time.ParseDuration(actualDuration)
		expectedValue, expectedErr := time.ParseDuration(expectedDuration)
		if !actualOK || !expectedOK || actualErr != nil || expectedErr != nil || actualValue != expectedValue {
			return false
		}
	}
	if isCA, exists := actual["isCA"]; exists && isCA != false {
		return false
	}
	if revisionLimit, exists := actual["revisionHistoryLimit"]; exists {
		if value, ok := revisionLimit.(int64); !ok || value < 1 {
			return false
		}
	}
	actualKey, ok := actual["privateKey"].(map[string]interface{})
	if !ok {
		return false
	}
	expectedKey := expected["privateKey"].(map[string]interface{})
	for field := range actualKey {
		if field != "algorithm" && field != "size" && field != "rotationPolicy" && field != "encoding" {
			return false
		}
	}
	for _, field := range []string{"algorithm", "size", "rotationPolicy"} {
		if !equality.Semantic.DeepEqual(actualKey[field], expectedKey[field]) {
			return false
		}
	}
	if encoding, exists := actualKey["encoding"]; exists && encoding != "PKCS1" && encoding != "PKCS8" {
		return false
	}
	return true
}

func certificateReady(certificate *unstructured.Unstructured) bool {
	conditions, _, _ := unstructured.NestedSlice(certificate.Object, "status", "conditions")
	for _, item := range conditions {
		if condition, ok := item.(map[string]interface{}); ok && condition["type"] == "Ready" && condition["status"] == "True" {
			return true
		}
	}
	return false
}
