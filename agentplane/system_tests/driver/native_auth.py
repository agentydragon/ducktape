"""Fixture identity names shared by the rendered chart and native clients."""

NATIVE_CLIENT_NAME = "system-test-native-client"
NATIVE_CLIENT_SVID_SECRET = "app-workload-tls"
RUNTIME_PROXY_TEST_READER_NAME = "system-test-runtime-proxy-reader"
RUNTIME_PROXY_TEST_READER_URI = f"spiffe://agentplane/{RUNTIME_PROXY_TEST_READER_NAME}"
RUNTIME_PROXY_TEST_READER_CERTIFICATE = f"{RUNTIME_PROXY_TEST_READER_NAME}-workload"
RUNTIME_PROXY_TEST_READER_SECRET = f"{RUNTIME_PROXY_TEST_READER_NAME}-workload-tls"
