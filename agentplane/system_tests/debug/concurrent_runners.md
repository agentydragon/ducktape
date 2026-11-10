# Concurrent scenario runner evaluation, 2026-10-30

The system suites share an expensive k3s deployment. Independent cases need their own Sandbox,
clients and scripted responses, while keeping shared async resources alive until every case has
cleaned up. A replacement for the current TaskGroups must also preserve individual results,
filtering, bounded concurrency and exclusive execution for cases that change deployment-wide state.

These probes used Bazel/RBE with Python 3.14.4, pytest 9.1.1 and the repository's root conftest.
They used an actual aiohttp server and per-case httpx clients, with an asyncio barrier to prove
overlap and an assertion that resources stayed on the same event loop. They did not start k3s:
both candidates failed a cheaper prerequisite for using our environment safely.

## pytest-asyncio-cooperative 0.40.0

Unlike the separately evaluated pytest-asyncio-concurrent plugin, this plugin did execute the first
two parameterized cases concurrently and produce separate JUnit results
([probe](https://app.buildbuddy.io/invocation/c3b8a74d-786f-41bf-8715-e1ef792b072d)).
Its documented incompatibility with pytest-asyncio requires `-p no:asyncio`; the probes used that
option, `--max-asyncio-tasks=2` and `--asyncio-task-timeout=4`.

Adding a second pair of cases exposed a fixture lifetime failure
([probe](https://app.buildbuddy.io/invocation/c03cdabe-3416-46b6-b88b-3f003176917a)). The fixture shape was:

```python
@pytest.fixture(scope="session")
async def shared():
    async with environment() as env:
        yield env

@pytest.fixture(params=["claude", "codex"])
def harness(request):
    return request.param

@pytest.fixture
async def case(shared, harness):
    async with shared.case(harness) as client:
        yield client

@pytest.mark.asyncio_cooperative
async def test_pair(shared, harness, case):
    await shared.pair(harness, case)

@pytest.mark.asyncio_cooperative
async def test_second_pair(shared, harness, case):
    await shared.pair(harness, case)
```

The first pair passed. During the second pair, the event artifact recorded `environment-stop`
with Claude still active. One case failed with a barrier timeout; the other reported an unhandled
async fixture during setup. This is incompatible with sharing one live stack across a bounded queue
of cases. Its fixture implementation replaces pytest's normal caching and finalization with its own
reference counting; passing one batch is insufficient evidence of session-scope compatibility.

Do not adopt this version. Failure/timeout recovery and exclusive scheduling were not evaluated
further after the shared-resource lifetime requirement failed.

## TestFlows core 2.0.261028.1173122

The probe launched a Bazel `py_binary` with native async TestFlows scenarios; pytest only checked
the child process's result. TestFlows provides scenario identities, parallel scheduling, joins,
context cleanup and native logs. It would require migrating fixture ownership to explicit suite
contexts. No JUnit exporter was found in the inspected core source, so Bazel's per-case XML reporting
would need a separate integration check or adapter.

The binary failed importing TestFlows on Python 3.14 because its module initialization calls
`asyncio.get_event_loop()` before a loop exists
([probe](https://app.buildbuddy.io/invocation/c3b8a74d-786f-41bf-8715-e1ef792b072d)).
Importing it from an active loop passed that point but failed importing `_compression` from its
vendored lzma implementation
([probe](https://app.buildbuddy.io/invocation/c03cdabe-3416-46b6-b88b-3f003176917a)).
The loop adjustment was only an exploratory compatibility check; it did not establish correct
TestFlows scheduling on our loop.

Do not adopt this version on the current toolchain. Its concurrency, failure cleanup, timeouts,
filtering and reports could not be tested at runtime because scenario execution never started.
We did not downgrade Python or patch its internal scheduler and standard-library compatibility.

## Recommendation

Keep the TaskGroup implementation for now. Neither evaluated package supplies a usable replacement
on our current dependencies, and adding compatibility patches would make us responsible for their
fixture/scheduler internals.

If individual collected cases become the priority, the next framework spike should use
pytest-xdist with an explicitly owned external stack. A normal xdist session fixture runs once per
worker, so changing only pytest flags would duplicate k3s. The parent must own stack startup and
teardown; workers receive connection information and create independent case resources. The shared
scripted-model server needs case routing, since its current listener is session-scoped and its
server is function-scoped. Deployment-wide mutations need an exclusive execution phase. This is
an architectural option, not something these probes validated.

An in-process runner would preserve direct async fixture sharing, but would have to own case
selection, resource lifetimes, timeouts and JUnit reporting. That is a larger commitment than the
current TaskGroup helper. Framework replacement alone also cannot remove serial provisioning or
cleanup inside a scenario; those costs should be measured independently.
