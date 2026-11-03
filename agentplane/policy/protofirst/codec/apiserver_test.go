package codec

import (
	"context"
	"net"
	"path/filepath"
	"testing"

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
