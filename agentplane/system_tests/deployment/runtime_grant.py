"""The PolicySet `test_egress` grants a running Sandbox. The k3s fixture creates it, so the scenario
binds no policy an operator's Sandboxes use; the scenario reads what it admits off the PolicySet."""

POLICY_SET = "test-runtime-grant"
