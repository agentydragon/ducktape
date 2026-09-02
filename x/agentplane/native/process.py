"""Direct stdin/stdout/stderr pipes to one native harness process, recorded as JSONL."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from io import BufferedReader
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field


class TextRecord(BaseModel):
    time_ns: int = Field(ge=0)
    text: str


def write_jsonl(path: Path, value: BaseModel) -> None:
    with path.open("ab") as output:
        output.write(value.model_dump_json().encode() + b"\n")
        output.flush()


def text(data: bytes) -> str:
    """Store native evidence as its UTF-8 wire text, not a redundant base64 copy."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("native evidence must be UTF-8 text") from error


def text_record(data: bytes) -> TextRecord:
    return TextRecord(time_ns=time.monotonic_ns(), text=text(data))


@contextmanager
def serve[Server: ThreadingHTTPServer](server: Server) -> Iterator[Server]:
    """Run a loopback HTTP server on a daemon thread for the lifetime of the block."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


class NativeProcess:
    """Drive a native harness over its own JSON frames. No PTY, facade, or lifecycle model."""

    def __init__(self, logs: Path, command: list[str], *, cwd: Path, environment: dict[str, str]):
        self.logs, self.command, self.cwd, self.environment = logs, command, cwd, environment
        self.process: subprocess.Popen[bytes] | None = None
        self.frames: queue.Queue[str] = queue.Queue()
        self.threads: list[threading.Thread] = []
        self.frame_handler: Callable[[dict[str, Any]], BaseModel | None] | None = None
        self._stdin_lock = threading.Lock()

    def start(self) -> None:
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.threads = [
            threading.Thread(target=self._stdout, daemon=True),
            threading.Thread(target=self._stderr, daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def __enter__(self) -> NativeProcess:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def write(self, frame: BaseModel) -> None:
        assert self.process is not None
        assert self.process.stdin is not None
        payload = frame.model_dump_json(by_alias=True).encode()
        write_jsonl(self.logs / "stdin.jsonl", text_record(payload))
        with self._stdin_lock:
            self.process.stdin.write(payload + b"\n")
            self.process.stdin.flush()

    def await_frame(self, predicate: Callable[[dict[str, Any]], bool], *, timeout: float) -> dict[str, Any]:
        end = time.monotonic() + timeout
        while (remaining := end - time.monotonic()) > 0:
            try:
                raw_frame = self.frames.get(timeout=remaining)
            except queue.Empty:
                break
            frame = json.loads(raw_frame)
            if not isinstance(frame, dict):
                raise ValueError("native stdout frame must be a JSON object")
            if predicate(frame):
                return frame
        raise TimeoutError("expected native frame was not observed")

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def close(self) -> int | None:
        if self.process is None:
            return None
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            result = self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            result = self.process.wait(timeout=5)
        for thread in self.threads:
            thread.join(timeout=5)
        return result

    def stdout_frames(self) -> list[dict[str, Any]]:
        records = (self.logs / "stdout.jsonl").read_text().splitlines()
        return [json.loads(json.loads(line)["text"]) for line in records]

    def stderr_text(self) -> str:
        records = (self.logs / "stderr.jsonl").read_text().splitlines()
        return "".join(json.loads(line)["text"] for line in records)

    def _stdout(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        for line in self.process.stdout:
            data = line.rstrip(b"\r\n")
            value = text(data)
            write_jsonl(self.logs / "stdout.jsonl", TextRecord(time_ns=time.monotonic_ns(), text=value))
            frame = json.loads(value)
            if not isinstance(frame, dict):
                raise ValueError("native stdout frame must be a JSON object")
            if self.frame_handler is not None:
                response = self.frame_handler(frame)
                if response is not None:
                    self.write(response)
            self.frames.put(value)

    def _stderr(self) -> None:
        assert self.process is not None
        assert self.process.stderr is not None
        stderr = cast(BufferedReader, self.process.stderr)
        while chunk := stderr.read1(65536):
            write_jsonl(self.logs / "stderr.jsonl", text_record(chunk))
