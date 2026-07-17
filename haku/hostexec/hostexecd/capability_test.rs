//! Cross-language capability verification against real Python-signed vectors.
//!
//! The three JWTs and the public PEM below are emitted deterministically by
//! `haku/hostexec/test_capability.py::test_emit_rust_vector` (fixed seed + fixed exps). If the
//! capability JWT payload changes, regenerate all four together (run that test with
//! `--test_output=all`). Verifying a genuine console-minted token here is the actual proof that
//! the Python signer and this Rust verifier interoperate.

use capability::{CapabilityError, RunAs, verify_capability};

const PUBLIC_PEM: &[u8] =
    b"-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAebVWLo/mVPlAeLES6KmLp5AfhTrmlb7X4OORC60ElmQ=\n-----END PUBLIC KEY-----\n";

// aud=hostexec-capability, host=wyrm2, run_as=root, argv=[bash,-lc,echo hi], exp=4102444800 (2100).
const JWT_VALID: &str = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJob3N0ZXhlYy1jYXBhYmlsaXR5IiwiaG9zdCI6Ind5cm0yIiwicnVuX2FzIjoicm9vdCIsImFyZ3YiOlsiYmFzaCIsIi1sYyIsImVjaG8gaGkiXSwiY3dkIjoiL2hvbWUvYWdlbnR5ZHJhZ29uIiwibm9uY2UiOiJ2ZWN0b3Itbm9uY2UiLCJleHAiOjQxMDI0NDQ4MDB9.qYk5ybVs4RjDNdXpKhz6dtF53W-W_ZHCEbRfp4tzAxSZuOqD09f7K8RTcmDkNlx8Vc4-aqSHbRAXhQcdM4IeBQ";

// Same key/claims but exp=1000000000 (2001) — expired.
const JWT_EXPIRED: &str = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJob3N0ZXhlYy1jYXBhYmlsaXR5IiwiaG9zdCI6Ind5cm0yIiwicnVuX2FzIjoicm9vdCIsImFyZ3YiOlsiYmFzaCIsIi1sYyIsImVjaG8gaGkiXSwiY3dkIjoiL2hvbWUvYWdlbnR5ZHJhZ29uIiwibm9uY2UiOiJ2IiwiZXhwIjoxMDAwMDAwMDAwfQ.E5IgQBaDqP-Pvm2PZ7049CSz_AlREZ0St8xg9nnYpTxQgtS2UsTKl_7EOW5cbx0WWnMb45EL-jGMB1AV-kuTCg";

// Same key/claims but aud=not-a-capability.
const JWT_WRONGAUD: &str = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJub3QtYS1jYXBhYmlsaXR5IiwiaG9zdCI6Ind5cm0yIiwicnVuX2FzIjoicm9vdCIsImFyZ3YiOlsiYmFzaCIsIi1sYyIsImVjaG8gaGkiXSwiY3dkIjoiL2hvbWUvYWdlbnR5ZHJhZ29uIiwibm9uY2UiOiJ2IiwiZXhwIjo0MTAyNDQ0ODAwfQ.RCNIjK9Ww9TnP4RKqEh86_AqVC7BzQpky35pvOZN6-U-5SLFe23evy3wtk1KWOanDFz1AbKlWey4bb4XgQgUAQ";

fn approved_argv() -> Vec<String> {
    vec!["bash".to_string(), "-lc".to_string(), "echo hi".to_string()]
}

#[test]
fn verifies_python_signed_capability() {
    let claims = verify_capability(
        JWT_VALID,
        PUBLIC_PEM,
        "wyrm2",
        RunAs::Root,
        &approved_argv(),
    )
    .unwrap();
    assert_eq!(claims.host, "wyrm2");
    assert_eq!(claims.run_as, RunAs::Root);
    assert_eq!(claims.argv, approved_argv());
    assert_eq!(claims.cwd.as_deref(), Some("/home/agentydragon"));
    assert_eq!(claims.nonce, "vector-nonce");
}

#[test]
fn rejects_wrong_host() {
    let r = verify_capability(
        JWT_VALID,
        PUBLIC_PEM,
        "rugged",
        RunAs::Root,
        &approved_argv(),
    );
    assert!(matches!(r, Err(CapabilityError::RunAsMismatch { .. })));
}

#[test]
fn rejects_wrong_run_as() {
    let r = verify_capability(
        JWT_VALID,
        PUBLIC_PEM,
        "wyrm2",
        RunAs::Agentydragon,
        &approved_argv(),
    );
    assert!(matches!(r, Err(CapabilityError::RunAsMismatch { .. })));
}

#[test]
fn rejects_argv_swap() {
    let bad = vec!["rm".to_string(), "-rf".to_string(), "/".to_string()];
    let r = verify_capability(JWT_VALID, PUBLIC_PEM, "wyrm2", RunAs::Root, &bad);
    assert!(matches!(r, Err(CapabilityError::ArgvMismatch)));
}

#[test]
fn rejects_tampered_signature() {
    let tampered = format!("{}x", &JWT_VALID[..JWT_VALID.len() - 1]);
    let r = verify_capability(
        &tampered,
        PUBLIC_PEM,
        "wyrm2",
        RunAs::Root,
        &approved_argv(),
    );
    assert!(matches!(r, Err(CapabilityError::Token(_))));
}

#[test]
fn rejects_expired() {
    let r = verify_capability(
        JWT_EXPIRED,
        PUBLIC_PEM,
        "wyrm2",
        RunAs::Root,
        &approved_argv(),
    );
    assert!(matches!(r, Err(CapabilityError::Token(_))));
}

#[test]
fn rejects_wrong_audience() {
    let r = verify_capability(
        JWT_WRONGAUD,
        PUBLIC_PEM,
        "wyrm2",
        RunAs::Root,
        &approved_argv(),
    );
    assert!(matches!(r, Err(CapabilityError::Token(_))));
}
