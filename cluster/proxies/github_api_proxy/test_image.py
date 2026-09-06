import pytest_bazel

from util.oci import OciImage, load_oci_image
from util.testing.container_logs import LoggedContainer


def test_image_entrypoint_as_unprivileged_user() -> None:
    tag = load_oci_image(OciImage("_main/cluster/proxies/github_api_proxy/image_layout.rloc", "github-api-proxy:test"))
    with LoggedContainer(tag, test_name="proxy-image-entrypoint", command=["--help"], network_mode="none") as container:
        wrapped = container.get_wrapped_container()
        assert wrapped.wait(timeout=15)["StatusCode"] == 0
        assert b"--config" in wrapped.logs()
        assert wrapped.attrs["Config"]["User"] == "1000:1000"


if __name__ == "__main__":
    pytest_bazel.main()
