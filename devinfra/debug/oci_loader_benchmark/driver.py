"""RBE comparison driver for the OCI loader investigation."""

import subprocess
import sys

from third_party.containers import postgres_18
from util.bazel import runfiles
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
    image = OciImage(postgres_18.IMAGE.rloc_file, tag)
    if mode == "python":
        load_oci_image(image)
        return
    if mode != "guarded_rules" or len(script) != 1:
        raise ValueError((mode, script))
    layout_rloc = runfiles.get_required_path(image.rloc_file).read_text().strip()
    wanted = read_oci_layout_digest(runfiles.get_required_path(layout_rloc))
    if _already_loaded(tag, wanted):
        return
    with _one_loader_at_a_time(tag):
        if _already_loaded(tag, wanted):
            return
        subprocess.run([script[0]], check=True)
        _loaded_marker(tag).write_text(f"{wanted} {daemon_image_id(tag)}")


if __name__ == "__main__":
    main()
