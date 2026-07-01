from collections.abc import Collection
from dataclasses import dataclass

import pytest
import pytest_bazel

from haku.gmail_labeling.backend import GmailLabelBackend


@dataclass
class _FakeRequest:
    thread_id: str
    body: dict[str, list[str]]


class _FakeThreads:
    def modify(self, *, userId, id, body):  # noqa: N803 -- mirrors Gmail's kwarg casing
        return _FakeRequest(thread_id=id, body=body)


class _FakeUsers:
    def threads(self):
        return _FakeThreads()


class _FakeBatchRequest:
    def __init__(self, callback, fail_thread_ids: Collection[str]) -> None:
        self._callback = callback
        self._fail_thread_ids = fail_thread_ids
        self.requests: list[tuple[str, _FakeRequest]] = []

    def add(self, request, *, request_id):
        self.requests.append((request_id, request))

    def execute(self):
        for request_id, _request in self.requests:
            if request_id in self._fail_thread_ids:
                self._callback(request_id, None, RuntimeError(f"boom: {request_id}"))
            else:
                self._callback(request_id, {}, None)


class _FakeService:
    def __init__(self, fail_thread_ids: Collection[str] = frozenset()) -> None:
        self._fail_thread_ids = fail_thread_ids
        self.batches: list[_FakeBatchRequest] = []

    def users(self):
        return _FakeUsers()

    def new_batch_http_request(self, callback):
        batch = _FakeBatchRequest(callback, self._fail_thread_ids)
        self.batches.append(batch)
        return batch


def test_modify_threads_sends_one_batch_within_limit():
    service = _FakeService()
    backend = GmailLabelBackend(service)
    backend.modify_threads(["t1", "t2"], add=["Label_1"], remove=[])
    assert len(service.batches) == 1
    assert {request_id for request_id, _ in service.batches[0].requests} == {"t1", "t2"}


def test_modify_threads_chunks_over_the_batch_limit():
    service = _FakeService()
    backend = GmailLabelBackend(service)
    thread_ids = [f"t{i}" for i in range(150)]
    backend.modify_threads(thread_ids, add=["Label_1"], remove=[])
    assert [len(batch.requests) for batch in service.batches] == [100, 50]


def test_modify_threads_raises_exception_group_on_partial_failure():
    service = _FakeService(fail_thread_ids={"t2"})
    backend = GmailLabelBackend(service)
    with pytest.raises(ExceptionGroup) as exc_info:
        backend.modify_threads(["t1", "t2", "t3"], add=["Label_1"], remove=[])
    assert len(exc_info.value.exceptions) == 1
    assert "boom: t2" in str(exc_info.value.exceptions[0])


def test_modify_threads_body_carries_add_and_remove():
    service = _FakeService()
    backend = GmailLabelBackend(service)
    backend.modify_threads(["t1"], add=["Label_1"], remove=["Label_2"])
    ((_request_id, request),) = service.batches[0].requests
    assert request.body == {"addLabelIds": ["Label_1"], "removeLabelIds": ["Label_2"]}


if __name__ == "__main__":
    pytest_bazel.main()
