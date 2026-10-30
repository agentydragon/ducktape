import httpx
import pytest
import pytest_bazel

from agentplane.egress.rule_client import Credential, Policy, Rule, Rules, StringList, Target, load_rules


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("/api/*", "/api/query?x=1", True),
        ("/api/*", "/api/nested/query", False),
        ("/api/**", "/api/nested/query", True),
        ("/api/[a]", "/api/a", False),
    ],
)
def test_path_matching(pattern: str, path: str, matches: bool) -> None:
    rule = Rule(hosts=["*.example.test"], methods=StringList(values=["POST"]), paths=StringList(values=[pattern]))
    assert rule.matches(host="DB.EXAMPLE.TEST", method="post", path=path) is matches
    assert not rule.matches(host="example.test", method="POST", path=path)
    assert not rule.matches(host=".example.test", method="POST", path=path)
    assert not rule.matches(host="db.example.test", method="GET", path=path)


def test_empty_and_absent_restrictions() -> None:
    assert Rule(hosts=["*"]).matches(host="db.example.test", method="POST", path="/")
    assert not Rule(hosts=["*"], methods=StringList()).matches(host="db.example.test", method="POST", path="/")
    assert not Rule(hosts=["*"], paths=StringList()).matches(host="db.example.test", method="POST", path="/")


def test_live_wire_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    # JSON shape from EgressRules.GetRules' grpc-gateway facade.
    monkeypatch.setenv("AGENTPLANE_EGRESS_URL", "http://egress.example.test")
    monkeypatch.setenv("AGENTPLANE_EGRESS_CREDENTIAL", "inert-workload-placeholder")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://egress.example.test/v1/rules"
        assert request.headers["Authorization"] == "Bearer inert-workload-placeholder"
        return httpx.Response(
            200,
            json={
                "policies": [
                    {
                        "name": "test",
                        "rules": [
                            {
                                "hosts": ["db.example.test"],
                                "methods": {"values": ["POST"]},
                                "paths": None,
                                "credential": {
                                    "name": "test-reader",
                                    "placeholder": "inert-test-placeholder",
                                    "targets": [{"method": "METHOD_BASIC_PASSWORD", "header": "Authorization"}],
                                },
                            }
                        ],
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        rules = load_rules(client=client)
    arguments = {"host": "db.example.test", "method": "POST", "path": "/api/query"}
    assert (
        rules.find_credential(**arguments, target_method="METHOD_BASIC_PASSWORD").placeholder
        == "inert-test-placeholder"
    )
    with pytest.raises(LookupError):
        rules.find_credential(**arguments, target_method="METHOD_SCHEME_TOKEN", scheme="Bearer")
    with pytest.raises(LookupError):
        Rules().find_credential(**arguments, target_method="METHOD_BASIC_PASSWORD")


def test_credential_ambiguity_and_identity_selection() -> None:
    rules = Rules(
        policies=[
            Policy(
                name="test",
                rules=[
                    Rule(
                        hosts=["db.example.test"],
                        credential=Credential(
                            name=name,
                            placeholder=f"inert-{name}",
                            targets=[Target(method="METHOD_BASIC_PASSWORD", header="Authorization")],
                        ),
                    )
                    for name in ["first", "second"]
                ],
            )
        ]
    )
    with pytest.raises(LookupError):
        rules.find_credential(host="db.example.test", method="POST", path="/", target_method="METHOD_BASIC_PASSWORD")
    assert (
        rules.find_credential(
            host="db.example.test", method="POST", path="/", target_method="METHOD_BASIC_PASSWORD", name="second"
        ).placeholder
        == "inert-second"
    )


if __name__ == "__main__":
    pytest_bazel.main()
