use claim::{ClaimedExecution, decode_run_request};
use serde_json::json;

#[test]
fn preserves_lease_when_backend_payload_is_incompatible() {
    let claim: ClaimedExecution = serde_json::from_value(json!({
        "execution_id": "7f263ec4-5c02-4428-b40f-eaca977d7042",
        "backend": "hostexec",
        "payload": {
            "token": "operator-token",
            "run_as": "agentydragon",
            "argv": ["true"],
            "cwd": null,
            "max_bytes": 1000,
            "timeout_ms": 5000
        },
        "lease_token": "lease-token",
        "lease_expires_at": "2026-07-21T04:48:35Z"
    }))
    .unwrap();

    assert_eq!(claim.execution_id, "7f263ec4-5c02-4428-b40f-eaca977d7042");
    assert_eq!(claim.lease_token, "lease-token");
    assert_eq!(
        decode_run_request(claim.payload).unwrap_err().to_string(),
        "missing field `cmd`"
    );
}

#[test]
fn decodes_current_backend_payload_after_envelope() {
    let claim: ClaimedExecution = serde_json::from_value(json!({
        "execution_id": "7f263ec4-5c02-4428-b40f-eaca977d7042",
        "backend": "hostexec",
        "payload": {
            "token": "operator-token",
            "run_as": "agentydragon",
            "cmd": "true",
            "cwd": null,
            "max_bytes": 1000,
            "timeout_ms": 5000
        },
        "lease_token": "lease-token",
        "lease_expires_at": "2026-07-21T04:48:35Z"
    }))
    .unwrap();

    let request = decode_run_request(claim.payload).unwrap();
    assert_eq!(request.cmd, "true");
}
