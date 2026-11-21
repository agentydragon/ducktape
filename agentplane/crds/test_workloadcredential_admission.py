"""Exercise the WorkloadCredential schema through Kubernetes admission and CEL: the required
Identity subject, binding union, and transition rule that changes a subject only with a new binding."""

from typing import Any

import pytest_bazel

from agentplane.testing.crd_admission import CrdAdmission, crd_namespace

NAMESPACE = "workload-credential-admission"
FIRST_POD = "3b1c0f1e-7a52-4f0c-9d52-2d7a54f0b2a1"
SECOND_POD = "5d0e6c7a-1b2f-4c3d-8e9f-0a1b2c3d4e5f"
SUBJECT = {"identity": {"name": "test-agent"}}
OTHER_SUBJECT = {"identity": {"name": "test-other-agent"}}


async def _unions(records: CrdAdmission) -> None:
    identity = {"identity": {"name": "test-agent", "uid": FIRST_POD}}
    # Pinning the Identity's UID is the requester's choice; a record from git names none.
    unpinned_identity = {"identity": {"name": "test-agent"}}
    for name, spec in (
        ("test-standing", {"subject": SUBJECT}),
        ("test-pinned-identity", {"subject": identity}),
        ("test-standing-identity", {"subject": unpinned_identity}),
        ("test-bound-identity", {"subject": SUBJECT, "binding": {"podUID": FIRST_POD}}),
        (
            "test-pod",
            {"subject": identity, "binding": {"podUID": FIRST_POD}, "pod": {"name": "test-pod", "uid": FIRST_POD}},
        ),
        ("test-vmi", {"subject": identity, "binding": {"vmiUID": FIRST_POD}}),
        (
            "test-vmi-launched",
            {
                "subject": identity,
                "binding": {"vmiUID": FIRST_POD},
                "pod": {"name": "test-launcher", "uid": SECOND_POD},
            },
        ),
    ):
        assert (await records.create(name, spec))["spec"] == spec
    invalid: tuple[dict[str, Any], ...] = (
        {},
        {"subject": {}},
        {"subject": {"serviceAccount": "test-agent"}},
        {"subject": {"identity": {"name": ""}}},
        {"subject": {"identity": {"name": "test-agent", "uid": "not-a-uid"}}},
        {"subject": SUBJECT, "binding": {}},
        {"subject": SUBJECT, "binding": {"podUID": FIRST_POD, "vmiUID": SECOND_POD}},
        {"subject": SUBJECT, "binding": {"podUID": "not-a-uid"}},
        # The caller's Pod is the bound one, and there is none without a binding.
        {"subject": SUBJECT, "binding": {"podUID": FIRST_POD}, "pod": {"name": "test-pod", "uid": SECOND_POD}},
        {"subject": SUBJECT, "pod": {"name": "test-pod", "uid": FIRST_POD}},
    )
    for spec in invalid:
        await records.rejects_create(spec)
    # The Certificate and Secret names derived from the record's name must stay within 253.
    await records.rejects_create({"subject": SUBJECT}, name="a" * 201)


async def _transitions(records: CrdAdmission) -> None:
    record = await records.create("test-transitions", {"subject": SUBJECT})
    # A record without a binding is replaced, never re-pointed.
    await records.rejects_spec(record, {"subject": OTHER_SUBJECT})

    record = await records.replace_spec(record, {"subject": SUBJECT, "binding": {"podUID": FIRST_POD}})
    for spec in (
        # The same incarnation cannot change who it acts as.
        {"subject": OTHER_SUBJECT, "binding": {"podUID": FIRST_POD}},
        # Clearing the binding is not a new one.
        {"subject": OTHER_SUBJECT},
    ):
        await records.rejects_spec(record, spec)
    record = await records.replace_spec(record, {"subject": OTHER_SUBJECT, "binding": {"podUID": SECOND_POD}})
    assert record["spec"]["subject"] == OTHER_SUBJECT

    # A VMI's launcher Pod is named once, and only a new binding names another.
    vmi = {"vmiUID": FIRST_POD}
    record = await records.replace_spec(record, {"subject": OTHER_SUBJECT, "binding": vmi})
    launcher = {"name": "test-launcher", "uid": SECOND_POD}
    record = await records.replace_spec(record, {"subject": OTHER_SUBJECT, "binding": vmi, "pod": launcher})
    await records.rejects_spec(
        record, {"subject": OTHER_SUBJECT, "binding": vmi, "pod": {**launcher, "uid": FIRST_POD}}
    )

    # The binding moves on its own: clearing it is how an owner revokes its incarnation.
    record = await records.replace_spec(record, {"subject": OTHER_SUBJECT})
    assert "binding" not in record["spec"]


async def _status(records: CrdAdmission) -> None:
    record = await records.create("test-status", {"subject": SUBJECT, "binding": {"podUID": FIRST_POD}})
    status = {
        "observedGeneration": 1,
        "credentialID": "0123456789abcdef0123456789abcdef",
        "uri": f"spiffe://agentplane/{NAMESPACE}/test-status/0123456789abcdef0123456789abcdef",
        "secretName": "svid-test-status-0123456789abcdef0123456789abcdef",
        "acceptedBinding": {"podUID": FIRST_POD},
    }
    record = await records.replace_status(record, status)
    assert record["status"] == status
    await records.rejects_status(record, {**status, "credentialID": "not-hex"})


async def test_workload_credential_admission() -> None:
    async with crd_namespace(NAMESPACE) as namespace:
        records = namespace.kind("workloadcredentials", "WorkloadCredential")
        await _unions(records)
        await _transitions(records)
        await _status(records)


if __name__ == "__main__":
    pytest_bazel.main()
