package codec

import (
	"context"
	"encoding/json"
	"net"
	"path/filepath"
	"testing"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/policy/protofirst"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/bazelbuild/rules_go/go/runfiles"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/dynamic"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

const probeGroup = "protofirst.agentplane.allegedly.works"

type probeServer struct {
	pb.UnimplementedPolicyProbeServer
	objects client.Client
}

func (s probeServer) GetPolicySet(ctx context.Context, request *pb.GetPolicySetRequest) (*pb.PolicySetResult, error) {
	obj := &PolicySet{}
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: request.GetNamespace(), Name: request.GetName()}, obj); err != nil {
		return nil, err
	}
	return &pb.PolicySetResult{Namespace: obj.Namespace, Name: obj.Name, Uid: string(obj.UID), Spec: proto.Clone(obj.Spec).(*pb.PolicySet)}, nil
}

func TestGeneratedCRDTypedClientAndGRPC(t *testing.T) {
	path, err := runfiles.Rlocation("ducktape/agentplane/policy/protofirst/kubernetes/customresourcedefinitions.gen.yaml")
	if err != nil {
		t.Fatal(err)
	}
	server, err := kubeapiserver.Start(func(e *envtest.Environment) { e.CRDDirectoryPaths = append(e.CRDDirectoryPaths, filepath.Dir(path)) })
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := server.Stop(); err != nil {
			t.Error(err)
		}
	})
	ctx := t.Context()
	if _, err := server.Clientset.CoreV1().Namespaces().Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "policy-probe"}}, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	gv := schema.GroupVersion{Group: probeGroup, Version: "v1alpha1"}
	scheme := runtime.NewScheme()
	scheme.AddKnownTypeWithName(gv.WithKind("PolicySet"), &PolicySet{})
	metav1.AddToGroupVersion(scheme, gv)
	objects, err := client.New(server.Config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	// A manifest-style writer knows only the Kubernetes JSON shape. The typed protobuf
	// wrapper must read the object it created without a second authored spec type.
	dynamicClient, err := dynamic.NewForConfig(server.Config)
	if err != nil {
		t.Fatal(err)
	}
	resources := dynamicClient.Resource(gv.WithResource("policysets")).Namespace("policy-probe")
	manifest := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": gv.String(), "kind": "PolicySet",
		"metadata": map[string]any{"name": "manifest"},
		"spec":     map[string]any{"rules": []any{map[string]any{"type": "egress", "hosts": []any{"api.example.com"}}}},
	}}
	if _, err := resources.Create(ctx, manifest, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	var fromManifest PolicySet
	if err := objects.Get(ctx, types.NamespacedName{Namespace: "policy-probe", Name: "manifest"}, &fromManifest); err != nil {
		t.Fatal(err)
	}
	if fromManifest.Spec.GetRules()[0].GetHosts()[0] != "api.example.com" {
		t.Fatalf("manifest read: %v", fromManifest.Spec)
	}
	crd, err := dynamicClient.Resource(schema.GroupVersionResource{Group: "apiextensions.k8s.io", Version: "v1", Resource: "customresourcedefinitions"}).Get(ctx, "policysets."+probeGroup, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	versions, found, err := unstructured.NestedSlice(crd.Object, "spec", "versions")
	if err != nil || !found || len(versions) != 1 {
		t.Fatalf("CRD versions = %v, found %v, error %v", versions, found, err)
	}
	version := versions[0].(map[string]any)
	policyPath := []string{"schema", "openAPIV3Schema", "properties", "spec", "properties", "rules", "items", "properties", "policy", "properties"}
	for _, check := range []struct {
		path []string
		want string
	}{
		{append(append([]string{}, policyPath...), "actions", "description"), "Actions lists allowed Action names by ActionGroup key."},
		{append(append([]string{}, policyPath...), "actions", "additionalProperties", "properties", "names", "description"), "Names contains the allowed Action names for this group."},
	} {
		got, found, err := unstructured.NestedString(version, check.path...)
		if err != nil || !found || got != check.want {
			t.Errorf("description at %v = %q, found %v, error %v; want %q", check.path, got, found, err, check.want)
		}
	}
	rulePath := []string{"schema", "openAPIV3Schema", "properties", "spec", "properties", "rules", "items", "properties"}
	for _, check := range []struct {
		path []string
		want string
	}{
		{append(append([]string{}, rulePath...), "namespaces", "x-kubernetes-list-type"), "set"},
		{append(append([]string{}, rulePath...), "targets", "properties", "sandboxes", "x-kubernetes-list-type"), "map"},
	} {
		got, found, err := unstructured.NestedString(version, check.path...)
		if err != nil || !found || got != check.want {
			t.Errorf("list semantics at %v = %q, found %v, error %v; want %q", check.path, got, found, err, check.want)
		}
	}
	keys, found, err := unstructured.NestedStringSlice(version, append(append([]string{}, rulePath...), "targets", "properties", "sandboxes", "x-kubernetes-list-map-keys")...)
	if err != nil || !found || len(keys) != 1 || keys[0] != "name" {
		t.Errorf("list map keys = %v, found %v, error %v", keys, found, err)
	}
	watchCtx, cancelWatch := context.WithTimeout(ctx, 30*time.Second)
	defer cancelWatch()
	stream, err := resources.Watch(watchCtx, metav1.ListOptions{FieldSelector: "metadata.name=action"})
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Stop()
	readEvent := func(want watch.EventType) *PolicySet {
		t.Helper()
		select {
		case event, ok := <-stream.ResultChan():
			if !ok || event.Type != want {
				t.Fatalf("watch event = %+v, open %v, want %s", event, ok, want)
			}
			data, err := json.Marshal(event.Object)
			if err != nil {
				t.Fatal(err)
			}
			var decoded PolicySet
			if err := json.Unmarshal(data, &decoded); err != nil {
				t.Fatalf("decode watch event: %v", err)
			}
			return &decoded
		case <-watchCtx.Done():
			t.Fatalf("waiting for %s: %v", want, watchCtx.Err())
			return nil
		}
	}
	action := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": gv.String(), "kind": "PolicySet",
		"metadata": map[string]any{"name": "action"},
		"spec": map[string]any{"rules": []any{map[string]any{
			"type": "action_auto_approve", "policy": map[string]any{
				"type": "argument_schema", "actions": map[string]any{"github": map[string]any{"names": []any{"get_file_contents"}}},
				"schema": map[string]any{"type": "object", "properties": map[string]any{"path": map[string]any{"type": "string"}}, "required": []any{"path"}},
			},
		}}},
	}}
	if _, err := resources.Create(ctx, action, metav1.CreateOptions{}); err != nil {
		t.Fatalf("create action policy: %v", err)
	}
	if got := readEvent(watch.Added).Spec.GetRules()[0].GetPolicy().GetActions()["github"].GetNames()[0]; got != "get_file_contents" {
		t.Fatalf("added watch spec = %s", got)
	}
	var actionRead PolicySet
	if err := objects.Get(ctx, types.NamespacedName{Namespace: "policy-probe", Name: "action"}, &actionRead); err != nil {
		t.Fatal(err)
	}
	actionPolicy := actionRead.Spec.GetRules()[0].GetPolicy()
	if actionPolicy.GetActions()["github"].GetNames()[0] != "get_file_contents" || actionPolicy.GetSchema().GetFields()["properties"].GetStructValue().GetFields()["path"].GetStructValue().GetFields()["type"].GetStringValue() != "string" {
		t.Fatalf("action policy or arbitrary JSON Schema did not survive Kubernetes: %v", actionPolicy)
	}
	updatedAction, err := resources.Get(ctx, "action", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	updatedRules, _, _ := unstructured.NestedSlice(updatedAction.Object, "spec", "rules")
	updatedRules[0].(map[string]any)["policy"].(map[string]any)["actions"].(map[string]any)["github"].(map[string]any)["names"] = []any{"list_commits"}
	if err := unstructured.SetNestedSlice(updatedAction.Object, updatedRules, "spec", "rules"); err != nil {
		t.Fatal(err)
	}
	if _, err := resources.Update(ctx, updatedAction, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if got := readEvent(watch.Modified).Spec.GetRules()[0].GetPolicy().GetActions()["github"].GetNames()[0]; got != "list_commits" {
		t.Fatalf("modified watch spec = %s", got)
	}
	if err := resources.Delete(ctx, "action", metav1.DeleteOptions{}); err != nil {
		t.Fatal(err)
	}
	if got := readEvent(watch.Deleted).Name; got != "action" {
		t.Fatalf("deleted watch name = %s", got)
	}
	for _, tc := range []struct {
		name  string
		names []any
	}{
		{"empty-list", []any{}},
		{"empty-name", []any{""}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			badAction := action.DeepCopy()
			badAction.SetName(tc.name)
			rules, _, _ := unstructured.NestedSlice(badAction.Object, "spec", "rules")
			policy := rules[0].(map[string]any)["policy"].(map[string]any)
			policy["actions"].(map[string]any)["github"].(map[string]any)["names"] = tc.names
			if err := unstructured.SetNestedSlice(badAction.Object, rules, "spec", "rules"); err != nil {
				t.Fatal(err)
			}
			if _, err := resources.Create(ctx, badAction, metav1.CreateOptions{}); !apierrors.IsInvalid(err) {
				t.Errorf("invalid Action names admitted, error = %v", err)
			}
		})
	}
	for _, tc := range []struct {
		name   string
		change func(map[string]any)
	}{
		{"missing-actions", func(policy map[string]any) { delete(policy, "actions") }},
		{"empty-actions", func(policy map[string]any) { policy["actions"] = map[string]any{} }},
		{"missing-names", func(policy map[string]any) { policy["actions"] = map[string]any{"github": map[string]any{}} }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			badAction := action.DeepCopy()
			badAction.SetName(tc.name)
			rules, _, _ := unstructured.NestedSlice(badAction.Object, "spec", "rules")
			tc.change(rules[0].(map[string]any)["policy"].(map[string]any))
			if err := unstructured.SetNestedSlice(badAction.Object, rules, "spec", "rules"); err != nil {
				t.Fatal(err)
			}
			if _, err := resources.Create(ctx, badAction, metav1.CreateOptions{}); !apierrors.IsInvalid(err) {
				t.Errorf("invalid Action policy admitted, error = %v", err)
			}
		})
	}
	for _, tc := range []struct {
		name  string
		rule  map[string]any
		valid bool
	}{
		{"role-binding", map[string]any{"type": "kubernetes_role_binding", "role": map[string]any{"kind": "Role", "name": "read"}, "namespaces": []any{"a", "b"}}, true},
		{"duplicate-namespaces", map[string]any{"type": "kubernetes_role_binding", "role": map[string]any{"kind": "Role", "name": "read"}, "namespaces": []any{"a", "a"}}, false},
		{"cluster-role", map[string]any{"type": "kubernetes_cluster_role_binding", "clusterRole": "read"}, true},
		{"inbox-access", map[string]any{"type": "session_inbox_access", "operations": []any{"send_message"}, "targets": map[string]any{"sandboxes": []any{map[string]any{"name": "one"}, map[string]any{"name": "two"}}}}, true},
		{"duplicate-targets", map[string]any{"type": "session_inbox_access", "operations": []any{"send_message"}, "targets": map[string]any{"sandboxes": []any{map[string]any{"name": "same"}, map[string]any{"name": "same"}}}}, false},
		{"missing-target-key", map[string]any{"type": "session_inbox_access", "operations": []any{"send_message"}, "targets": map[string]any{"sandboxes": []any{map[string]any{}}}}, false},
		{"blanket-private", map[string]any{"type": "egress", "hosts": []any{"*"}, "clusterInternal": true}, false},
		{"invalid-host", map[string]any{"type": "egress", "hosts": []any{"INVALID HOST"}}, false},
		{"invalid-fields", map[string]any{"type": "egress", "hosts": []any{"example.com"}, "clusterRole": "read"}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			resource := manifest.DeepCopy()
			resource.SetName(tc.name)
			if err := unstructured.SetNestedSlice(resource.Object, []any{tc.rule}, "spec", "rules"); err != nil {
				t.Fatal(err)
			}
			_, err := resources.Create(ctx, resource, metav1.CreateOptions{})
			if tc.valid && err != nil {
				t.Fatalf("valid rule rejected: %v", err)
			}
			if !tc.valid && !apierrors.IsInvalid(err) {
				t.Fatalf("invalid rule admitted, error = %v", err)
			}
			if tc.valid {
				var typed PolicySet
				if err := objects.Get(ctx, types.NamespacedName{Namespace: "policy-probe", Name: tc.name}, &typed); err != nil {
					t.Fatal(err)
				}
				if typed.Spec.GetRules()[0].GetType() != tc.rule["type"] {
					t.Fatalf("rule type lost: %v", typed.Spec)
				}
			}
		})
	}
	obj := &PolicySet{TypeMeta: metav1.TypeMeta{APIVersion: gv.String(), Kind: "PolicySet"}, ObjectMeta: metav1.ObjectMeta{Namespace: "policy-probe", Name: "sample"}, Spec: &pb.PolicySet{Rules: []*pb.Rule{{Type: "egress", Hosts: []string{"api.example.com"}}}}}
	if err := objects.Create(ctx, obj); err != nil {
		t.Fatal(err)
	}
	var got PolicySet
	if err := objects.Get(ctx, client.ObjectKeyFromObject(obj), &got); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(obj.Spec, got.Spec) || got.UID == "" {
		t.Fatalf("typed read = %+v", got)
	}
	bad := &PolicySet{TypeMeta: obj.TypeMeta, ObjectMeta: metav1.ObjectMeta{Namespace: "policy-probe", Name: "bad"}, Spec: &pb.PolicySet{Rules: []*pb.Rule{{Type: "egress"}}}}
	if err := objects.Create(ctx, bad); !apierrors.IsInvalid(err) {
		t.Fatalf("invalid egress admitted: %v", err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	grpcServer := grpc.NewServer()
	pb.RegisterPolicyProbeServer(grpcServer, probeServer{objects: objects})
	go grpcServer.Serve(listener)
	t.Cleanup(grpcServer.Stop)
	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	result, err := pb.NewPolicyProbeClient(conn).GetPolicySet(ctx, &pb.GetPolicySetRequest{Namespace: "policy-probe", Name: "sample"})
	if err != nil {
		t.Fatal(err)
	}
	if result.GetUid() != string(got.UID) || !proto.Equal(result.GetSpec(), got.Spec) {
		t.Fatalf("gRPC result = %v", result)
	}
}
