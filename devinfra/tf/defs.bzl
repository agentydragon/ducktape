"""`tf_module` that declares its `tf_providers_versions` from the same provider map."""

load("@rules_tf//tf:def.bzl", "tf_providers_versions", _tf_module = "tf_module")

def tf_module(name, providers, tf_version = "1.12.6", **kwargs):
    """rules_tf's `tf_module` plus the `:providers` target it reads its provider versions from.

    Args:
        name: Module target name.
        providers: Provider name to `"namespace/type:constraint"`; the module uses every one.
        tf_version: OpenTofu version recorded in the generated `versions.tf.json`.
        **kwargs: Passed to rules_tf's `tf_module`.
    """
    tf_providers_versions(name = "providers", providers = providers, tf_version = tf_version)
    _tf_module(name = name, providers = providers.keys(), providers_versions = ":providers", **kwargs)
