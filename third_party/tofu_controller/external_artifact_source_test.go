package controllers

import (
	"context"
	"testing"

	"github.com/fluxcd/pkg/apis/meta"
	sourcev1 "github.com/fluxcd/source-controller/api/v1"
	. "github.com/onsi/gomega"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	infrav1 "github.com/flux-iac/tofu-controller/api/v1alpha2"
)

func externalArtifactTerraform(name, artifact, lastAttemptedRevision string) *infrav1.Terraform {
	return &infrav1.Terraform{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "tf"},
		Spec: infrav1.TerraformSpec{
			SourceRef: infrav1.CrossNamespaceSourceReference{
				Kind:      sourcev1.ExternalArtifactKind,
				Name:      artifact,
				Namespace: "flux-system",
			},
		},
		Status: infrav1.TerraformStatus{LastAttemptedRevision: lastAttemptedRevision},
	}
}

func newExternalArtifactReconciler(t *testing.T, objs ...runtime.Object) *TerraformReconciler {
	scheme := runtime.NewScheme()
	NewWithT(t).Expect(sourcev1.AddToScheme(scheme)).To(Succeed())
	NewWithT(t).Expect(infrav1.AddToScheme(scheme)).To(Succeed())
	r := &TerraformReconciler{}
	r.Client = fake.NewClientBuilder().
		WithScheme(scheme).
		WithRuntimeObjects(objs...).
		WithIndex(&infrav1.Terraform{}, infrav1.ExternalArtifactIndexKey, r.IndexBy(sourcev1.ExternalArtifactKind)).
		Build()
	return r
}

func moduleArtifact(revision string) *sourcev1.ExternalArtifact {
	return &sourcev1.ExternalArtifact{
		ObjectMeta: metav1.ObjectMeta{Name: "module-a", Namespace: "flux-system"},
		Status: sourcev1.ExternalArtifactStatus{
			Artifact: &meta.Artifact{Revision: revision, URL: "http://source-watcher.flux-system.svc/module-a.tar.gz"},
		},
	}
}

func TestGetSourceResolvesExternalArtifact(t *testing.T) {
	g := NewWithT(t)
	r := newExternalArtifactReconciler(t, moduleArtifact("latest@sha256:aaa"))

	source, err := r.getSource(context.Background(), externalArtifactTerraform("a", "module-a", ""))

	g.Expect(err).NotTo(HaveOccurred())
	g.Expect(source).To(BeAssignableToTypeOf(&sourcev1.ExternalArtifact{}))
	g.Expect(source.GetArtifact().Revision).To(Equal("latest@sha256:aaa"))
}

// A new ExternalArtifact revision enqueues exactly the Terraforms that reference it and have
// not already attempted that revision.
func TestExternalArtifactRevisionChangeEnqueuesReferencingTerraforms(t *testing.T) {
	g := NewWithT(t)
	artifact := moduleArtifact("latest@sha256:bbb")
	r := newExternalArtifactReconciler(
		t,
		artifact,
		externalArtifactTerraform("stale", "module-a", "latest@sha256:aaa"),
		externalArtifactTerraform("current", "module-a", "latest@sha256:bbb"),
		externalArtifactTerraform("other-module", "module-b", "latest@sha256:aaa"),
	)

	reqs := r.requestsForRevisionChangeOf(infrav1.ExternalArtifactIndexKey)(context.Background(), artifact)

	g.Expect(reqs).To(ConsistOf(reconcile.Request{NamespacedName: types.NamespacedName{Namespace: "tf", Name: "stale"}}))
}
