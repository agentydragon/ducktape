import pytest
import pytest_bazel

from devinfra.ci.check_rust_versions import check_versions


def test_matching_version_with_reformatted_toolchain_call() -> None:
    check_versions('rust.toolchain(versions = ["1.100.0"], edition = "2024")', "rustfmt-1.100.0-x86_64.tar.gz")


@pytest.mark.parametrize(
    ("module", "source", "message"),
    [
        ('rust.toolchain(versions = ["1.99.0"])', "rustfmt-1.100.0-x86_64.tar.gz", "differs"),
        ('rust.toolchain(versions = ["1.100.0"])', "unexpected-source", "unexpected Fenix"),
        ("rust.toolchain()", "rustfmt-1.100.0-x86_64.tar.gz", "expected exactly one literal"),
        (
            'rust.toolchain(versions = ["1.100.0"])\nrust.toolchain(versions = ["1.100.0"])',
            "rustfmt-1.100.0-x86_64.tar.gz",
            "expected exactly one rust.toolchain",
        ),
    ],
)
def test_missing_ambiguous_or_mismatched_versions_fail(module: str, source: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_versions(module, source)


if __name__ == "__main__":
    pytest_bazel.main()
