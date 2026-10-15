"""Integration-test the provisioner with llama.cpp against local HTTP fixtures."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import ClassVar


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    counts: ClassVar[dict[str, int]] = {}
    range_requests: ClassVar[list[str]] = []
    content = b"GGUF fixture model bytes\n" * 8192
    resume_prefix = 65536

    def do_HEAD(self) -> None:
        self.counts[self.path] = self.counts.get(self.path, 0) + 1
        if self.path == "/missing":
            self.send_error(http.HTTPStatus.NOT_FOUND)
            return
        self.send_response(http.HTTPStatus.OK)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(self.content)))
        self.end_headers()

    def do_GET(self) -> None:
        self.counts[self.path] = self.counts.get(self.path, 0) + 1
        requested_range = self.headers.get("Range")
        if requested_range is not None:
            self.range_requests.append(requested_range)
        if self.path == "/missing":
            self.send_error(http.HTTPStatus.NOT_FOUND)
            return
        if self.path == "/resume":
            payload = self.content
            if requested_range is not None:
                start = int(requested_range.removeprefix("bytes=").split("-", maxsplit=1)[0])
                payload = payload[start:]
                self.send_response(http.HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{len(self.content) - 1}/{len(self.content)}")
            else:
                self.send_response(http.HTTPStatus.OK)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if requested_range is None:
                # Interrupt the first response after a prefix. llama.cpp should keep
                # its temporary download and resume from the next byte on retry.
                self.wfile.write(payload[: self.resume_prefix])
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(payload)
            return
        payload = b"incorrect payload length matches\n" if self.path == "/wrong" else self.content
        self.send_response(http.HTTPStatus.OK)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def artifact(url: str, content: bytes, *, digest: str | None = None) -> dict[str, object]:
    return {"url": url, "sha256": digest or hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}


def write_manifest(path: Path, artifacts: dict[str, dict[str, object]]) -> None:
    path.write_text(json.dumps({"version": 1, "artifacts": artifacts}))


def run_provisioner(helper: Path, manifest: Path, store: Path, ollama_blobs: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(name, None)
    return subprocess.run(
        [str(helper), str(manifest), str(store), str(ollama_blobs)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    helper = Path(sys.argv[1])
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        with tempfile.TemporaryDirectory(prefix="rugged-llama-provisioner-") as temp:
            root = Path(temp)
            store = root / "model-store"
            ollama_blobs = root / "ollama" / "blobs"
            ollama_blobs.mkdir(parents=True)
            digest = hashlib.sha256(FixtureHandler.content).hexdigest()

            # An existing Ollama blob is verified and linked without any HTTP request.
            source = ollama_blobs / f"sha256-{digest}"
            source.write_bytes(FixtureHandler.content)
            manifest = root / "manifest.json"
            write_manifest(manifest, {"fixture-model": artifact(f"{base_url}/never", FixtureHandler.content)})
            before = dict(FixtureHandler.counts)
            result = run_provisioner(helper, manifest, store, ollama_blobs)
            require(result.returncode == 0, f"Ollama cache import failed: {result.stderr}")
            target = store / "blobs" / f"sha256-{digest}"
            require(target.read_bytes() == FixtureHandler.content, "imported artifact bytes differ")
            require(target.stat().st_ino == source.stat().st_ino, "same-filesystem Ollama cache was not reused")
            require(FixtureHandler.counts == before, "Ollama cache import used the network")

            # A second verified local hit also bypasses llama.cpp and the network.
            result = run_provisioner(helper, manifest, store, ollama_blobs)
            require(result.returncode == 0, f"verified cache hit failed: {result.stderr}")
            require(FixtureHandler.counts == before, "verified cache hit used the network")
            # Missing artifacts download through the real llama.cpp binary and verify before promotion.
            downloaded = root / "downloaded-store"
            download_manifest = root / "download-manifest.json"
            write_manifest(
                download_manifest, {"download-model": artifact(f"{base_url}/download", FixtureHandler.content)}
            )
            result = run_provisioner(helper, download_manifest, downloaded, root / "no-ollama-blobs")
            require(result.returncode == 0, f"llama.cpp fixture download failed: {result.stderr}")
            downloaded_target = downloaded / "blobs" / f"sha256-{digest}"
            require(downloaded_target.read_bytes() == FixtureHandler.content, "downloaded bytes differ")
            require(FixtureHandler.counts.get("/download", 0) >= 1, "expected an actual llama.cpp request")
            require(
                not list((downloaded / ".staging").glob("*.partial")), "verified partial was not atomically promoted"
            )

            # A broken response leaves a llama.cpp temporary file; the retry must
            # request only the remaining bytes and still verify the whole blob.
            resumed_store = root / "resumed-store"
            resumed_manifest = root / "resumed-manifest.json"
            write_manifest(resumed_manifest, {"resume-model": artifact(f"{base_url}/resume", FixtureHandler.content)})
            FixtureHandler.range_requests.clear()
            result = run_provisioner(helper, resumed_manifest, resumed_store, root / "no-ollama-blobs")
            require(result.returncode == 0, f"llama.cpp partial resume failed: {result.stderr}")
            resumed_target = resumed_store / "blobs" / f"sha256-{digest}"
            require(resumed_target.read_bytes() == FixtureHandler.content, "resumed bytes differ")
            require(
                any(
                    request.startswith(f"bytes={FixtureHandler.resume_prefix}-")
                    for request in FixtureHandler.range_requests
                ),
                f"llama.cpp did not resume the interrupted transfer: {FixtureHandler.range_requests}",
            )

            # A failed integrity check cannot publish a blob.
            wrong_store = root / "wrong-store"
            wrong_manifest = root / "wrong-manifest.json"
            wrong_digest = "0" * 64 if digest != "0" * 64 else "1" * 64
            write_manifest(
                wrong_manifest,
                {
                    "wrong-model": artifact(
                        f"{base_url}/wrong/sha256:{wrong_digest}", FixtureHandler.content, digest=wrong_digest
                    )
                },
            )
            result = run_provisioner(helper, wrong_manifest, wrong_store, root / "no-ollama-blobs")
            require(result.returncode != 0, "wrong digest was accepted")
            require(not (wrong_store / "blobs" / f"sha256-{wrong_digest}").exists(), "wrong digest reached final cache")

            # A later network failure leaves a previously valid cache object unchanged.
            failure_manifest = root / "failure-manifest.json"
            missing_digest = "f" * 64 if digest != "f" * 64 else "e" * 64
            write_manifest(
                failure_manifest,
                {
                    "cached-model": artifact(f"{base_url}/unused", FixtureHandler.content),
                    "missing-model": artifact(f"{base_url}/missing", b"missing fixture", digest=missing_digest),
                },
            )
            failure_store = root / "failure-store"
            failure_store_target = failure_store / "blobs" / f"sha256-{digest}"
            failure_store_target.parent.mkdir(parents=True)
            failure_store_target.write_bytes(FixtureHandler.content)
            result = run_provisioner(helper, failure_manifest, failure_store, root / "no-ollama-blobs")
            require(result.returncode != 0, "HTTP 404 was treated as successful provisioning")
            require(
                failure_store_target.read_bytes() == FixtureHandler.content, "failed fetch changed valid cache data"
            )

            # A complete bad leftover partial is discarded, then replaced by a verified llama.cpp download.
            recovery_store = root / "recovery-store"
            recovery_partial = recovery_store / ".staging" / f"{digest}.partial"
            recovery_partial.parent.mkdir(parents=True)
            recovery_partial.write_bytes(b"x" * len(FixtureHandler.content))
            recovery_manifest = root / "recovery-manifest.json"
            write_manifest(
                recovery_manifest, {"recovery-model": artifact(f"{base_url}/recover", FixtureHandler.content)}
            )
            result = run_provisioner(helper, recovery_manifest, recovery_store, root / "no-ollama-blobs")
            require(result.returncode == 0, f"invalid complete partial prevented recovery: {result.stderr}")
            require(
                (recovery_store / "blobs" / f"sha256-{digest}").read_bytes() == FixtureHandler.content,
                "recovered artifact differs",
            )

    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()


if __name__ == "__main__":
    main()
