import hashlib
import sys
from pathlib import Path

from PIL import Image

from devinfra.buildbuddy.ci import invocation_ids
from devinfra.pr_visuals import artifacts, collection, publisher, visual_diff
from devinfra.pr_visuals.publisher import DownloadedAsset, DownloadedVariant, DownloadedVisualTest, build_bundle
from devinfra.testing import output_records


def main() -> None:
    wheel_root = Path(sys.argv[1]).resolve()
    for module in (artifacts, collection, publisher, invocation_ids, visual_diff, output_records):
        assert module.__file__ is not None
        assert Path(module.__file__).resolve().is_relative_to(wheel_root)
    fixture = Path(sys.argv[2])
    fixture.mkdir()
    screen = fixture / "screen.png"
    Image.new("RGB", (2, 2), "red").save(screen)
    digest = hashlib.sha256(screen.read_bytes()).hexdigest()
    bundle = build_bundle(
        [
            DownloadedVisualTest(
                target_label="//smoke:test",
                slug=publisher.target_slug("//smoke:test", repository="owner/repo"),
                title="Packaging smoke",
                assets=[
                    DownloadedAsset(
                        "smoke.py::test_screen", None, "screenshot", "screen", [DownloadedVariant(screen, digest, [])]
                    )
                ],
                configuration="smoke",
                issues=[],
            )
        ],
        Path(sys.argv[3]),
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        repository="owner/repo",
    )
    index_html = (bundle / "index.html").read_text()
    assert "Visual review" in index_html
    assert 'href="gallery.css"' in index_html
    assert 'src="gallery_sort.js"' in index_html
    assert (bundle / "gallery_slider.css").is_file()
    assert (bundle / "gallery_slider.js").is_file()
    assert (bundle / "gallery.css").is_file()
    assert (bundle / "gallery_sort.js").is_file()
    comment = publisher.success_comment_body(
        repository="owner/repo",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        url="https://visuals.example/",
        review_tests=[],
    )
    assert "<h2>Visual review for" in comment
    assert '<a href="https://visuals.example/index.html">Open visual review</a>' in comment
    test_page = (
        bundle / "tests" / publisher.target_slug("//smoke:test", repository="owner/repo") / "index.html"
    ).read_text()
    assert "Packaging smoke" in test_page
    assert "screen" in test_page
    assert 'href="../../gallery_slider.css"' in test_page
    assert 'src="../../gallery_slider.js"' in test_page


if __name__ == "__main__":
    main()
