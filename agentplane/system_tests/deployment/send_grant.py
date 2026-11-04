"""The PolicySet `test_agent_messaging` binds its sender with, and the inbox tag it may send to. The k3s
fixture creates the PolicySet, so the scenario binds no policy an operator's Sandboxes use."""

POLICY_SET = "test-send-to-messaging"
INBOX_TAG = "test-messaging"
