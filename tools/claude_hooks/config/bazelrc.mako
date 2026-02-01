# Bazel proxy configuration for Claude Code web (auto-generated)
# JVM proxy settings for Bazel server (BCR access, etc.)
startup --host_jvm_args=-Dhttps.proxyHost=127.0.0.1
startup --host_jvm_args=-Dhttps.proxyPort=${proxy_port}
startup --host_jvm_args=-Djavax.net.ssl.trustStore=${truststore_path | sh}
startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=${truststore_password | sh}

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
# Avoid gVisor linux-sandbox (/dev/null issues in CC web).
# remote,local: prefer remote execution (BuildBuddy RBE) when configured, fall
# back to unsandboxed local execution.  Remote workers don't use gVisor.
build --spawn_strategy=remote,local
test --spawn_strategy=remote,local

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
