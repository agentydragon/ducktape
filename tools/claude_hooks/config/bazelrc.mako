# Bazel proxy configuration for Claude Code web (auto-generated)
# JVM proxy settings for Bazel server (BCR access, etc.)
startup --host_jvm_args=-Dhttps.proxyHost=127.0.0.1
startup --host_jvm_args=-Dhttps.proxyPort=${proxy_port}
startup --host_jvm_args=-Djavax.net.ssl.trustStore=${truststore_path | sh}
startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=${truststore_password | sh}

# Pass proxy + TLS CA to repository rules (for Go modules in gazelle, etc.)
# GONOPROXY=* forces all Go module downloads through HTTP proxy.
# Explicitly NOT passing NO_PROXY since it excludes *.googleapis.com
# (see also _strip_no_proxy_google in env_file.py which fixes this globally).
common --repo_env=HTTP_PROXY
common --repo_env=HTTPS_PROXY
common --repo_env=http_proxy
common --repo_env=https_proxy
common --repo_env=GONOPROXY=*
common --repo_env=GOPRIVATE=
common --repo_env=GOSUMDB=sum.golang.org
# TLS inspection CA: the egress proxy MITMs HTTPS, so git and Go need the
# proxy CA in their trust stores.  GIT_SSL_CAINFO covers git-ls-remote
# (used by Gazelle fetch_repo), SSL_CERT_FILE covers Go's net/http and most
# other tools that repo rules shell out to.  These are --repo_env so they
# only affect the Bazel host (repo rules); RBE workers are unaffected.
common --repo_env=GIT_SSL_CAINFO=${combined_ca_path | sh}
common --repo_env=SSL_CERT_FILE=${combined_ca_path | sh}
% if buildbuddy_configured:
# Enable RBE: host_platform for CC toolchain resolution, spawn_strategy for
# gVisor avoidance (remote workers don't use gVisor, local fallback is
# unsandboxed). See .bazelrc for the full flag set.
build --config=rbe
% else:
# BuildBuddy not configured - use local execution only.
# Set host_platform for CC toolchain resolution (BAZEL_DO_NOT_DETECT_CPP_TOOLCHAIN=1
# disables auto-detection), but without remote execution.
build --host_platform=//:rbe_linux_x64
build --spawn_strategy=local
% endif

# Tag invocations for BuildBuddy filtering
build --build_metadata=ROLE=claude-code

# Skip live OpenAI tests in wildcard expansion (no API key available)
test --test_tag_filters=-live_openai_api
% if local_registry_path:

# Local registry with patched ape module (native ELF instead of APE binaries)
# This avoids binfmt_misc requirement in Claude Code web containers
# Note: Local registry is checked first, then BCR as fallback
common --registry=file://${local_registry_path | sh}
common --registry=https://bcr.bazel.build
% endif
