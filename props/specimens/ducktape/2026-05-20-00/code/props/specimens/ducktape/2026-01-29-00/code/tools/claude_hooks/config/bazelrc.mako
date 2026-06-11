# Bazel proxy configuration for Claude Code web (auto-generated)
# JVM proxy settings for Bazel server (BCR access, etc.)
startup --host_jvm_args=-Dhttps.proxyHost=127.0.0.1
startup --host_jvm_args=-Dhttps.proxyPort=${proxy_port}
startup --host_jvm_args=-Djavax.net.ssl.trustStore=${truststore_path | sh}
startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=${truststore_password | sh}

# Propagate proxy env vars into sandbox actions (for pip, uv, etc.)
build --action_env=HTTPS_PROXY=${local_proxy | sh}
build --action_env=HTTP_PROXY=${local_proxy | sh}
build --action_env=https_proxy=${local_proxy | sh}
build --action_env=http_proxy=${local_proxy | sh}

# Pass proxy to repository rules (for Go modules in gazelle, etc.)
# GONOPROXY=* forces all Go module downloads through HTTP proxy
# Explicitly NOT passing NO_PROXY since it excludes *.googleapis.com
common --repo_env=HTTP_PROXY
common --repo_env=HTTPS_PROXY
common --repo_env=http_proxy
common --repo_env=https_proxy
common --repo_env=GONOPROXY=*
common --repo_env=GOPRIVATE=
common --repo_env=GOSUMDB=sum.golang.org
# Propagate Node.js CA bundle into sandbox (for npm, puppeteer, etc.)
build --action_env=NODE_EXTRA_CA_CERTS=${combined_ca_path | sh}

# Use local execution instead of sandbox (sandbox has /dev/null issues in CC web)
build --spawn_strategy=local
test --spawn_strategy=local
% if local_registry_path:

# Local registry with patched ape module (native ELF instead of APE binaries)
# This avoids binfmt_misc requirement in Claude Code web containers
# Note: Local registry is checked first, then BCR as fallback
common --registry=file://${local_registry_path | sh}
common --registry=https://bcr.bazel.build
% endif
