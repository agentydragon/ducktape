package controller

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/cluster/providers/buildbuddy/apis/v1alpha1"
	"github.com/agentydragon/ducktape/cluster/providers/buildbuddy/internal/buildbuddy"
	"github.com/crossplane/crossplane-runtime/v2/pkg/meta"
	"github.com/crossplane/crossplane-runtime/v2/pkg/reconciler/managed"
	"github.com/crossplane/crossplane-runtime/v2/pkg/resource"
	xpv2 "github.com/crossplane/crossplane/apis/v2/core/v2"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	apiKeyConnectionSecretKey = "api-key"
	ambiguousExternalName     = "buildbuddy-api-key-create-ambiguous:"
)

type apiKeyConnector struct {
	kube             client.Client
	usage            *resource.ProviderConfigUsageTracker
	sourceSecret     string
	credentialSecret string
}

func (c *apiKeyConnector) Connect(ctx context.Context, cr *v1alpha1.APIKey) (managed.TypedExternalClient[*v1alpha1.APIKey], error) {
	if err := validateAPIKeyConnectionSecret(cr, c.sourceSecret, c.credentialSecret); err != nil {
		return nil, err
	}
	if err := c.usage.Track(ctx, cr); err != nil {
		return nil, fmt.Errorf("track provider config usage: %w", err)
	}
	apiKey, err := providerAPIKey(ctx, c.kube, cr.Namespace, cr.Spec.ProviderConfigReference, c.credentialSecret)
	if err != nil {
		return nil, err
	}
	api, err := buildbuddy.NewClient(string(apiKey))
	if err != nil {
		return nil, err
	}
	return &apiKeyExternal{kube: c.kube, api: api}, nil
}

func validateAPIKeyConnectionSecret(cr *v1alpha1.APIKey, sourceSecret, credentialSecret string) error {
	ref := cr.Spec.WriteConnectionSecretToReference
	if ref == nil || ref.Name == "" {
		return errors.New("writeConnectionSecretToRef is required for BuildBuddy API keys")
	}
	if ref.Name == sourceSecret || ref.Name == credentialSecret {
		return errors.New("API-key connection Secret must not replace the configured input or credential Secret")
	}
	return nil
}

type buildBuddyAPIKey interface {
	APIKeys(context.Context, string) ([]buildbuddy.APIKey, error)
	APIKeyValue(context.Context, string, string) ([]byte, error)
	CreateAPIKey(context.Context, string, string, []string, bool) (string, error)
	UpdateAPIKey(context.Context, string, string, []string, bool) error
	DeleteAPIKey(context.Context, string) error
	Close() error
}

type apiKeyExternal struct {
	kube client.Client
	api  buildBuddyAPIKey
}

func (e *apiKeyExternal) Observe(ctx context.Context, cr *v1alpha1.APIKey) (managed.ExternalObservation, error) {
	externalName := meta.GetExternalName(cr)
	if strings.HasPrefix(externalName, ambiguousExternalName) {
		return managed.ExternalObservation{}, errors.New("BuildBuddy API-key creation had an ambiguous result; inspect group-owned keys, then set the external-name to the key ID or clear the marker only after confirming no key was created")
	}
	groupID, err := apiKeyGroupID(cr)
	if err != nil {
		return managed.ExternalObservation{}, err
	}
	if externalName == "" {
		if meta.WasDeleted(cr) {
			return managed.ExternalObservation{ResourceExists: false}, nil
		}
		if err := e.outputSecretReadyForCreate(ctx, cr); err != nil {
			return managed.ExternalObservation{}, err
		}
		return managed.ExternalObservation{ResourceExists: false}, nil
	}
	key, exists, err := e.keyInGroup(ctx, groupID, externalName)
	if err != nil {
		return managed.ExternalObservation{}, err
	}
	if !exists {
		cr.Status.AtProvider = v1alpha1.APIKeyObservation{}
		if meta.WasDeleted(cr) {
			return managed.ExternalObservation{ResourceExists: false}, nil
		}
		if err := e.outputSecretReadyForCreate(ctx, cr); err != nil {
			return managed.ExternalObservation{}, err
		}
		return managed.ExternalObservation{ResourceExists: false}, nil
	}
	if meta.WasDeleted(cr) {
		return managed.ExternalObservation{ResourceExists: true}, nil
	}
	connectionDetails, err := e.connectionSecretDetails(ctx, cr, groupID, externalName)
	if err != nil {
		return managed.ExternalObservation{}, err
	}
	capabilities, err := observedCapabilities(key.Capabilities)
	if err != nil {
		return managed.ExternalObservation{}, err
	}
	cr.Status.AtProvider = v1alpha1.APIKeyObservation{
		GroupID:             groupID,
		Label:               key.Label,
		Capabilities:        capabilities,
		VisibleToDevelopers: key.VisibleToDevelopers,
	}
	wantedCapabilities, err := desiredCapabilities(cr.Spec.ForProvider.Capabilities)
	if err != nil {
		return managed.ExternalObservation{}, err
	}
	upToDate := key.Label == cr.Spec.ForProvider.Label && equalStrings(key.Capabilities, wantedCapabilities) && key.VisibleToDevelopers == cr.Spec.ForProvider.VisibleToDevelopers
	if upToDate {
		cr.Status.SetConditions(xpv2.Available())
	}
	return managed.ExternalObservation{
		ResourceExists:    true,
		ResourceUpToDate:  upToDate,
		ConnectionDetails: connectionDetails,
	}, nil
}

func (e *apiKeyExternal) Create(ctx context.Context, cr *v1alpha1.APIKey) (managed.ExternalCreation, error) {
	if meta.GetExternalName(cr) != "" {
		return managed.ExternalCreation{}, errors.New("external-name is set but the BuildBuddy API key was not found; refusing to create over an explicit external name")
	}
	if cr.UID == "" {
		return managed.ExternalCreation{}, errors.New("APIKey UID is required before creating a BuildBuddy key")
	}
	if err := validateAPIKeyConnectionSecret(cr, "", ""); err != nil {
		return managed.ExternalCreation{}, err
	}
	if err := e.outputSecretReadyForCreate(ctx, cr); err != nil {
		return managed.ExternalCreation{}, err
	}
	groupID, err := apiKeyGroupID(cr)
	if err != nil {
		return managed.ExternalCreation{}, err
	}
	capabilities, err := desiredCapabilities(cr.Spec.ForProvider.Capabilities)
	if err != nil {
		return managed.ExternalCreation{}, err
	}
	if cr.Spec.ForProvider.Label == "" {
		return managed.ExternalCreation{}, errors.New("BuildBuddy API-key label is required")
	}
	id, err := e.api.CreateAPIKey(ctx, groupID, cr.Spec.ForProvider.Label, capabilities, cr.Spec.ForProvider.VisibleToDevelopers)
	if err != nil {
		// Crossplane retries failed creates, but a failed response can still
		// mean that BuildBuddy created the key. Require an operator to resolve
		// the outcome before allowing another create.
		meta.SetExternalName(cr, ambiguousExternalName+string(cr.UID))
		return managed.ExternalCreation{}, fmt.Errorf("BuildBuddy API-key creation may have succeeded; inspect group-owned keys before retrying: %w", err)
	}
	if id == "" {
		// The client validates this too. Treat any incomplete response as
		// ambiguous because the remote key may already exist.
		meta.SetExternalName(cr, ambiguousExternalName+string(cr.UID))
		return managed.ExternalCreation{}, errors.New("BuildBuddy returned no API-key ID; automatic creation is blocked to avoid duplicating it")
	}
	// Let Crossplane persist the ID before the next observation reads the
	// value, so a readback failure cannot cause another create.
	meta.SetExternalName(cr, id)
	return managed.ExternalCreation{}, nil
}

func (e *apiKeyExternal) Update(ctx context.Context, cr *v1alpha1.APIKey) (managed.ExternalUpdate, error) {
	externalName := meta.GetExternalName(cr)
	if externalName == "" || strings.HasPrefix(externalName, ambiguousExternalName) {
		return managed.ExternalUpdate{}, errors.New("BuildBuddy API-key external-name is missing or unresolved")
	}
	groupID, err := apiKeyGroupID(cr)
	if err != nil {
		return managed.ExternalUpdate{}, err
	}
	if _, exists, err := e.keyInGroup(ctx, groupID, externalName); err != nil {
		return managed.ExternalUpdate{}, err
	} else if !exists {
		return managed.ExternalUpdate{}, errors.New("BuildBuddy API key is no longer present in its configured group")
	}
	connectionDetails, err := e.connectionSecretDetails(ctx, cr, groupID, externalName)
	if err != nil {
		return managed.ExternalUpdate{}, err
	}
	capabilities, err := desiredCapabilities(cr.Spec.ForProvider.Capabilities)
	if err != nil {
		return managed.ExternalUpdate{}, err
	}
	if err := e.api.UpdateAPIKey(ctx, externalName, cr.Spec.ForProvider.Label, capabilities, cr.Spec.ForProvider.VisibleToDevelopers); err != nil {
		return managed.ExternalUpdate{}, err
	}
	cr.Status.AtProvider = v1alpha1.APIKeyObservation{
		GroupID:             groupID,
		Label:               cr.Spec.ForProvider.Label,
		Capabilities:        append([]v1alpha1.APIKeyCapability(nil), cr.Spec.ForProvider.Capabilities...),
		VisibleToDevelopers: cr.Spec.ForProvider.VisibleToDevelopers,
	}
	return managed.ExternalUpdate{ConnectionDetails: connectionDetails}, nil
}

func (e *apiKeyExternal) Delete(ctx context.Context, cr *v1alpha1.APIKey) (managed.ExternalDelete, error) {
	externalName := meta.GetExternalName(cr)
	if externalName == "" {
		return managed.ExternalDelete{}, nil
	}
	if strings.HasPrefix(externalName, ambiguousExternalName) {
		return managed.ExternalDelete{}, errors.New("cannot safely delete an API key after an ambiguous create; resolve the external-name marker first")
	}
	groupID, err := apiKeyGroupID(cr)
	if err != nil {
		return managed.ExternalDelete{}, err
	}
	if _, exists, err := e.keyInGroup(ctx, groupID, externalName); err != nil {
		return managed.ExternalDelete{}, err
	} else if !exists {
		return managed.ExternalDelete{}, nil
	}
	return managed.ExternalDelete{}, e.api.DeleteAPIKey(ctx, externalName)
}

func (e *apiKeyExternal) Disconnect(context.Context) error { return e.api.Close() }

func (e *apiKeyExternal) keyInGroup(ctx context.Context, groupID, id string) (buildbuddy.APIKey, bool, error) {
	keys, err := e.api.APIKeys(ctx, groupID)
	if err != nil {
		return buildbuddy.APIKey{}, false, err
	}
	for _, key := range keys {
		if key.ID == id {
			return key, true, nil
		}
	}
	return buildbuddy.APIKey{}, false, nil
}

func (e *apiKeyExternal) outputSecretReadyForCreate(ctx context.Context, cr *v1alpha1.APIKey) error {
	ref := cr.Spec.WriteConnectionSecretToReference
	if ref == nil || ref.Name == "" {
		return errors.New("writeConnectionSecretToRef is required for BuildBuddy API keys")
	}
	secret := &corev1.Secret{}
	err := e.kube.Get(ctx, types.NamespacedName{Namespace: cr.Namespace, Name: ref.Name}, secret)
	if apierrors.IsNotFound(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("check API-key connection Secret: %w", err)
	}
	controller := metav1.GetControllerOf(secret)
	if controller == nil || controller.UID != cr.UID {
		return errors.New("API-key connection Secret already exists and is not controlled by this APIKey")
	}
	if len(secret.Data) != 0 {
		return errors.New("API-key connection Secret already contains data; refusing to create a key that cannot be safely published")
	}
	return nil
}

func (e *apiKeyExternal) connectionSecretDetails(ctx context.Context, cr *v1alpha1.APIKey, groupID, id string) (managed.ConnectionDetails, error) {
	ref := cr.Spec.WriteConnectionSecretToReference
	if ref == nil || ref.Name == "" {
		return nil, errors.New("writeConnectionSecretToRef is required for BuildBuddy API keys")
	}
	secret := &corev1.Secret{}
	if err := e.kube.Get(ctx, types.NamespacedName{Namespace: cr.Namespace, Name: ref.Name}, secret); err != nil {
		if !apierrors.IsNotFound(err) {
			return nil, fmt.Errorf("check API-key connection Secret: %w", err)
		}
	} else if controller := metav1.GetControllerOf(secret); controller == nil || controller.UID != cr.UID {
		return nil, errors.New("API-key connection Secret already exists and is not controlled by this APIKey")
	}
	value, err := e.api.APIKeyValue(ctx, groupID, id)
	if err != nil {
		return nil, err
	}
	if len(value) == 0 {
		return nil, errors.New("BuildBuddy API-key value readback is empty")
	}
	return managed.ConnectionDetails{apiKeyConnectionSecretKey: value}, nil
}

func apiKeyGroupID(cr *v1alpha1.APIKey) (string, error) {
	desired := cr.Spec.ForProvider.GroupID
	observed := cr.Status.AtProvider.GroupID
	if meta.WasDeleted(cr) && observed != "" {
		return observed, nil
	}
	if desired == "" {
		return "", errors.New("BuildBuddy API-key groupID is required")
	}
	if observed != "" && observed != desired {
		return "", errors.New("BuildBuddy API-key groupID cannot change after creation")
	}
	return desired, nil
}

func desiredCapabilities(capabilities []v1alpha1.APIKeyCapability) ([]string, error) {
	result := make([]string, 0, len(capabilities))
	seen := make(map[v1alpha1.APIKeyCapability]struct{}, len(capabilities))
	for _, capability := range capabilities {
		if !supportedCapability(capability) {
			return nil, fmt.Errorf("unsupported BuildBuddy API-key capability %q", capability)
		}
		if _, exists := seen[capability]; exists {
			return nil, fmt.Errorf("duplicate BuildBuddy API-key capability %q", capability)
		}
		seen[capability] = struct{}{}
		result = append(result, string(capability))
	}
	sort.Strings(result)
	return result, nil
}

func observedCapabilities(capabilities []string) ([]v1alpha1.APIKeyCapability, error) {
	result := make([]v1alpha1.APIKeyCapability, 0, len(capabilities))
	for _, capability := range capabilities {
		value := v1alpha1.APIKeyCapability(capability)
		if !supportedCapability(value) {
			return nil, fmt.Errorf("BuildBuddy returned unsupported API-key capability %q", capability)
		}
		result = append(result, value)
	}
	sort.Slice(result, func(i, j int) bool { return result[i] < result[j] })
	return result, nil
}

func supportedCapability(capability v1alpha1.APIKeyCapability) bool {
	switch capability {
	case v1alpha1.APIKeyCapabilityCacheWrite,
		v1alpha1.APIKeyCapabilityRegisterExecutor,
		v1alpha1.APIKeyCapabilityCASWrite,
		v1alpha1.APIKeyCapabilityOrgAdmin,
		v1alpha1.APIKeyCapabilityAuditLogRead,
		v1alpha1.APIKeyCapabilityRegisterCacheProxy,
		v1alpha1.APIKeyCapabilitySendNotification:
		return true
	default:
		return false
	}
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	left = append([]string(nil), left...)
	right = append([]string(nil), right...)
	sort.Strings(left)
	sort.Strings(right)
	for i := range left {
		if left[i] != right[i] {
			return false
		}
	}
	return true
}

func newAPIKeyReconciler(mgr ctrl.Manager, connector managed.TypedExternalConnector[*v1alpha1.APIKey]) *managed.Reconciler {
	return managed.NewReconciler(
		mgr, resource.ManagedKind(v1alpha1.APIKeyGroupVersionKind),
		managed.WithTypedExternalConnector[*v1alpha1.APIKey](connector),
		managed.WithPollInterval(time.Minute),
		managed.WithInitializers(),
		managed.WithManagementPolicies(),
	)
}

func SetupAPIKey(mgr ctrl.Manager, sourceSecret, credentialSecret string) error {
	r := newAPIKeyReconciler(mgr, &apiKeyConnector{
		kube:             mgr.GetClient(),
		usage:            resource.NewProviderConfigUsageTracker(mgr.GetClient(), &v1alpha1.ProviderConfigUsage{}),
		sourceSecret:     sourceSecret,
		credentialSecret: credentialSecret,
	})
	return ctrl.NewControllerManagedBy(mgr).Named("buildbuddy-api-key").For(&v1alpha1.APIKey{}).Complete(r)
}
