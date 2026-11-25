import pytest
import pytest_bazel

# gazelle:include_dep //devinfra/testing:syrupy_plugin

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("previous", ["current", "old"])
def test_exports_final_collection(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, previous: str) -> None:
    pytester.makepyfile(test_example='def test_value(snapshot): assert snapshot == "current"')
    path = pytester.path / "__snapshots__" / "test_example.ambr"
    path.parent.mkdir()
    path.write_text(
        f"# serializer version: 1\n# name: test_value\n  '{previous}'\n# ---\n# name: obsolete\n  1\n# ---\n"
    )
    outputs = pytester.path / "outputs"
    outputs.mkdir()
    monkeypatch.setenv("TEST_UNDECLARED_OUTPUTS_DIR", str(outputs))

    result = pytester.inline_run("-p", "devinfra.testing.syrupy_plugin", "--snapshot-update")
    assert result.ret == pytest.ExitCode.OK
    exported = (outputs / path.name).read_text()
    assert exported == path.read_text()
    assert "current" in exported
    assert "obsolete" not in exported


if __name__ == "__main__":
    pytest_bazel.main()
