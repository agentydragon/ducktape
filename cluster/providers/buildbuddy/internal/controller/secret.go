package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/cluster/providers/buildbuddy/apis/v1alpha1"
	"github.com/agentydragon/ducktape/cluster/providers/buildbuddy/internal/buildbuddy"
	"github.com/crossplane/crossplane-runtime/v2/pkg/meta"
	"github.com/crossplane/crossplane-runtime/v2/pkg/reconciler/managed"
	"github.com/crossplane/crossplane-runtime/v2/pkg/resource"
	xpv2 "github.com/crossplane/crossplane/apis/v2/core/v2"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	sourceIndex      = "spec.forProvider.valueSecretRef.name"
	reassertInterval = time.Hour
)

type connector struct {
	kube             client.Client
	usage            *resource.ProviderConfigUsageTracker
	sourceSecret     string
	credentialSecret string
}

func (c *connector) Connect(ctx context.Context, cr *v1alpha1.Secret) (managed.TypedExternalClient[*v1alpha1.Secret], error) {
	if cr.Spec.ForProvider.ValueSecretRef.Name != c.sourceSecret {
		return nil, errors.New("source secret is not allowed by provider configuration")
	}
	if err := c.usage.Track(ctx, cr); err != nil {
		return nil, fmt.Errorf("track provider config usage: %w", err)
	}
	ref := cr.GetProviderConfigReference()
	if ref == nil {
		return nil, errors.New("provider config reference is required")
	}
	var credentials v1alpha1.ProviderCredentials
	switch ref.Kind {
	case "ProviderConfig":
		pc := &v1alpha1.ProviderConfig{}
		if err := c.kube.Get(ctx, types.NamespacedName{Namespace: cr.Namespace, Name: ref.Name}, pc); err != nil {
			return nil, err
		}
		credentials = pc.Spec.Credentials
	case "ClusterProviderConfig":
		pc := &v1alpha1.ClusterProviderConfig{}
		if err := c.kube.Get(ctx, types.NamespacedName{Name: ref.Name}, pc); err != nil {
			return nil, err
		}
		credentials = pc.Spec.Credentials
	default:
		return nil, fmt.Errorf("unsupported provider config kind %q", ref.Kind)
	}
	if err := c.validateCredentials(cr.Namespace, credentials); err != nil {
		return nil, err
	}
	apiKey, err := resource.CommonCredentialExtractor(ctx, credentials.Source, c.kube, credentials.CommonCredentialSelectors)
	if err != nil {
		return nil, fmt.Errorf("read provider credentials: %w", err)
	}
	return &external{kube: c.kube, api: &buildbuddy.Client{URL: buildbuddy.DefaultURL, APIKey: string(apiKey)}}, nil
}

// Crossplane grants provider pods broad Secret RBAC. Do not let a managed
// resource use that privilege to read an arbitrary Secret.
func (c *connector) validateCredentials(namespace string, credentials v1alpha1.ProviderCredentials) error {
	if credentials.Source != xpv2.CredentialsSourceSecret || credentials.SecretRef == nil ||
		credentials.SecretRef.Namespace != namespace || credentials.SecretRef.Name != c.credentialSecret {
		return errors.New("provider credentials must reference the configured secret in the managed resource namespace")
	}
	return nil
}

type external struct {
	kube   client.Client
	api    *buildbuddy.Client
	source *corev1.Secret
	key    buildbuddy.PublicKey
}

func (e *external) Observe(ctx context.Context, cr *v1alpha1.Secret) (managed.ExternalObservation, error) {
	name := meta.GetExternalName(cr)
	if name == "" {
		return managed.ExternalObservation{}, errors.New("crossplane.io/external-name is required")
	}
	exists, err := e.api.Exists(ctx, name)
	if err != nil {
		return managed.ExternalObservation{}, err
	}
	if meta.WasDeleted(cr) {
		return managed.ExternalObservation{ResourceExists: exists}, nil
	}
	ref := cr.Spec.ForProvider.ValueSecretRef
	e.source = &corev1.Secret{}
	if err := e.kube.Get(ctx, types.NamespacedName{Namespace: cr.Namespace, Name: ref.Name}, e.source); err != nil {
		return managed.ExternalObservation{}, fmt.Errorf("read source secret: %w", err)
	}
	if len(e.source.Data[ref.Key]) == 0 {
		return managed.ExternalObservation{}, errors.New("source secret key is missing or empty")
	}
	e.key, err = e.api.PublicKey(ctx)
	if err != nil {
		return managed.ExternalObservation{}, err
	}
	if !exists {
		return managed.ExternalObservation{ResourceExists: false}, nil
	}
	observed := cr.Status.AtProvider
	upToDate := observed.SourceUID == string(e.source.UID) && observed.SourceResourceVersion == e.source.ResourceVersion && observed.SourceKey == ref.Key && observed.ExternalName == name && observed.PublicKeyID == e.key.ID && observed.LastAppliedAt != nil && time.Since(observed.LastAppliedAt.Time) < reassertInterval
	if upToDate {
		cr.Status.SetConditions(xpv2.Available())
	}
	return managed.ExternalObservation{ResourceExists: true, ResourceUpToDate: upToDate}, nil
}

func (e *external) write(ctx context.Context, cr *v1alpha1.Secret) error {
	return e.api.Put(ctx, meta.GetExternalName(cr), e.source.Data[cr.Spec.ForProvider.ValueSecretRef.Key], e.key)
}

func (e *external) Create(ctx context.Context, cr *v1alpha1.Secret) (managed.ExternalCreation, error) {
	// Create cannot persist status. The next observation requests one idempotent
	// update, which persists the source revision after a successful write.
	return managed.ExternalCreation{}, e.write(ctx, cr)
}

func (e *external) Update(ctx context.Context, cr *v1alpha1.Secret) (managed.ExternalUpdate, error) {
	if err := e.write(ctx, cr); err != nil {
		return managed.ExternalUpdate{}, err
	}
	cr.Status.AtProvider = v1alpha1.SecretObservation{SourceUID: string(e.source.UID), SourceResourceVersion: e.source.ResourceVersion, SourceKey: cr.Spec.ForProvider.ValueSecretRef.Key, ExternalName: meta.GetExternalName(cr), PublicKeyID: e.key.ID, LastAppliedAt: &metav1.Time{Time: time.Now()}}
	return managed.ExternalUpdate{}, nil
}

func (e *external) Delete(ctx context.Context, cr *v1alpha1.Secret) (managed.ExternalDelete, error) {
	return managed.ExternalDelete{}, e.api.Delete(ctx, meta.GetExternalName(cr))
}
func (e *external) Disconnect(context.Context) error { return nil }

func SetupSecret(mgr ctrl.Manager, sourceSecret, credentialSecret string) error {
	if err := mgr.GetFieldIndexer().IndexField(context.Background(), &v1alpha1.Secret{}, sourceIndex, func(obj client.Object) []string {
		return []string{obj.(*v1alpha1.Secret).Spec.ForProvider.ValueSecretRef.Name}
	}); err != nil {
		return err
	}
	watchSource := handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
		var secrets v1alpha1.SecretList
		if err := mgr.GetClient().List(ctx, &secrets, client.InNamespace(obj.GetNamespace()), client.MatchingFields{sourceIndex: obj.GetName()}); err != nil {
			ctrl.Log.Error(err, "list BuildBuddy secrets for changed source")
			return nil
		}
		requests := make([]reconcile.Request, 0, len(secrets.Items))
		for _, secret := range secrets.Items {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&secret)})
		}
		return requests
	})
	r := managed.NewReconciler(mgr, resource.ManagedKind(v1alpha1.SecretGroupVersionKind), managed.WithTypedExternalConnector[*v1alpha1.Secret](&connector{kube: mgr.GetClient(), usage: resource.NewProviderConfigUsageTracker(mgr.GetClient(), &v1alpha1.ProviderConfigUsage{}), sourceSecret: sourceSecret, credentialSecret: credentialSecret}), managed.WithPollInterval(time.Minute), managed.WithManagementPolicies())
	return ctrl.NewControllerManagedBy(mgr).Named("buildbuddy-secret").For(&v1alpha1.Secret{}).Watches(&corev1.Secret{}, watchSource).Complete(r)
}
