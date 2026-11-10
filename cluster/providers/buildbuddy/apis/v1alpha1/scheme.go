// Adapted from Crossplane provider-template; see LICENSE-APACHE-2.0.

package v1alpha1

import "github.com/crossplane/crossplane-runtime/v2/pkg/resource"

var (
	_ resource.ModernManaged            = &Secret{}
	_ resource.ManagedList              = &SecretList{}
	_ resource.ProviderConfig           = &ProviderConfig{}
	_ resource.ProviderConfig           = &ClusterProviderConfig{}
	_ resource.TypedProviderConfigUsage = &ProviderConfigUsage{}
	_ resource.ProviderConfigUsageList  = &ProviderConfigUsageList{}
)

func init() {
	SchemeBuilder.Register(&Secret{}, &SecretList{})
	SchemeBuilder.Register(&ProviderConfig{}, &ProviderConfigList{})
	SchemeBuilder.Register(&ProviderConfigUsage{}, &ProviderConfigUsageList{})
	SchemeBuilder.Register(&ClusterProviderConfig{}, &ClusterProviderConfigList{})
}
