"""RBE comparison driver for the OCI loader investigation."""

import subprocess
import sys

from devinfra.bazel import runfiles
from third_party.containers import postgres_18
from util.oci import (
    OciImage,
    _already_loaded,
    _loaded_marker,
    _one_loader_at_a_time,
    daemon_image_id,
    load_oci_image,
    read_oci_layout_digest,
)


def main() -> None:
    mode, *args = sys.argv[1:]
    if mode == "noop":
        return
    tag, *script = args
    image = OciImage(tag=tag, layout_rlocation=postgres_18.IMAGE.layout_rlocation)
    if mode == "python":
        load_oci_image(image)
        return
    if mode != "guarded_rules" or len(script) != 1:
        raise ValueError((mode, script))
    wanted = read_oci_layout_digest(runfiles.get_required_path(image.layout_rlocation))
    if _already_loaded(tag, wanted):
        return
    with _one_loader_at_a_time(tag):
        if _already_loaded(tag, wanted):
            return
        subprocess.run([script[0]], check=True)
        _loaded_marker(tag).write_text(f"{wanted} {daemon_image_id(tag)}")


if __name__ == "__main__":
    main()
