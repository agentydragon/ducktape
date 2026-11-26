"""Fixture identity names shared by the rendered chart and native clients."""

from agentplane.net.peertls.service_uris import RUNTIME_PROXY_TEST_READER_NAME

NATIVE_CLIENT_NAME = "system-test-native-client"
NATIVE_CLIENT_SVID_SECRET = "app-workload-tls"
RUNTIME_PROXY_TEST_READER_CERTIFICATE = f"{RUNTIME_PROXY_TEST_READER_NAME}-workload"
RUNTIME_PROXY_TEST_READER_SECRET = f"{RUNTIME_PROXY_TEST_READER_NAME}-workload-tls"
