import sys
from pathlib import Path

from devinfra.ci import invocation_ids
from devinfra.pr_visuals import publisher
from devinfra.pr_visuals.publisher import DownloadedVisualTest, build_bundle
from util import visual_diff, visual_review
from util.visual_review import VisualReviewAsset, VisualReviewManifest


def main() -> None:
    wheel_root = Path(sys.argv[1]).resolve()
    for module in (publisher, invocation_ids, visual_diff, visual_review):
        assert module.__file__ is not None
        assert Path(module.__file__).resolve().is_relative_to(wheel_root)
    fixture = Path(sys.argv[2])
    fixture.mkdir()
    (fixture / "screen.png").write_bytes(b"fixture png")
    manifest = VisualReviewManifest(
        title="Packaging smoke", assets=[VisualReviewAsset(path="screen.png", label="screen")]
    )
    bundle = build_bundle(
        [DownloadedVisualTest(target_label="//smoke:test", slug="smoke-test", manifest=manifest, directory=fixture)],
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
    test_page = (bundle / "tests/smoke-test/index.html").read_text()
    assert "Packaging smoke" in test_page
    assert "screen.png" in test_page
    assert 'href="../../gallery_slider.css"' in test_page
    assert 'src="../../gallery_slider.js"' in test_page


if __name__ == "__main__":
    main()
