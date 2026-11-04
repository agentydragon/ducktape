"""The credential `test_egress`'s probe discovers and presents. The k3s fixture creates a ServiceAccount
holding no RBAC, a token for it in the egress credentials namespace, the EgressCredential that substitutes
that token and the PolicySet that admits it to the API server's who-am-I; the API server then names the
account the presented token belongs to."""

POLICY_SET = "test-egress-probe"
CREDENTIAL = "test-egress-probe"
# The token's account, in the egress credentials namespace with its token.
SERVICE_ACCOUNT = "test-egress-probe-bot"
# The API server by a name `basic` does not spell, so that this PolicySet decides requests to it: the proxy
# matches hosts on the exact string.
HOST = "kubernetes.default.svc"
WHOAMI_PATH = "/apis/authentication.k8s.io/v1/selfsubjectreviews"
