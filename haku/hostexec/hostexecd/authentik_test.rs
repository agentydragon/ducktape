//! Authentik verifier tests: accept an authorized operator token; reject a missing/wrong group, a
//! wrong run_as, a wrong host audience, a wrong issuer, an expired token, and a tampered signature.
//!
//! Self-contained: signs RS256 test tokens with a fixed test RSA keypair (`testdata/`) and verifies
//! against its public key. In production the decoding key comes from Authentik's JWKS instead; the
//! verification logic is identical.

use authentik::{AuthError, verify_operator_token};
use jsonwebtoken::{Algorithm, DecodingKey, EncodingKey, Header, encode};
use serde::Serialize;

const PRIVATE_PEM: &[u8] = include_bytes!("testdata/rsa_test_private.pem");
const PUBLIC_PEM: &[u8] = include_bytes!("testdata/rsa_test_public.pem");
const ISSUER: &str = "https://auth.allegedly.works/application/o/hostexec-wyrm2/";
const SUBJECT: &str = "operator-agentydragon";
const FUTURE: u64 = 4102444800; // 2100-01-01
const PAST: u64 = 1000000000; // 2001

#[derive(Serialize)]
struct TestClaims {
    iss: String,
    aud: String,
    sub: String,
    groups: Vec<String>,
    exp: u64,
}

fn mint(iss: &str, aud: &str, groups: &[&str], exp: u64) -> String {
    let claims = TestClaims {
        iss: iss.to_string(),
        aud: aud.to_string(),
        sub: SUBJECT.to_string(),
        groups: groups.iter().map(|g| g.to_string()).collect(),
        exp,
    };
    let key = EncodingKey::from_rsa_pem(PRIVATE_PEM).unwrap();
    encode(&Header::new(Algorithm::RS256), &claims, &key).unwrap()
}

fn key() -> DecodingKey {
    DecodingKey::from_rsa_pem(PUBLIC_PEM).unwrap()
}

#[test]
fn accepts_authorized_operator() {
    let token = mint(ISSUER, "hostexec-wyrm2", &["hostexec-root-wyrm2"], FUTURE);
    let op = verify_operator_token(&token, &key(), ISSUER, "wyrm2", "root").unwrap();
    assert_eq!(op.subject, SUBJECT);
}

#[test]
fn rejects_missing_group() {
    let token = mint(ISSUER, "hostexec-wyrm2", &["hostexec-user-wyrm2"], FUTURE);
    let r = verify_operator_token(&token, &key(), ISSUER, "wyrm2", "root");
    assert!(matches!(r, Err(AuthError::NotAuthorized { .. })));
}

#[test]
fn rejects_wrong_run_as() {
    // Token authorizes root; a request to run as agentydragon needs the agentydragon group.
    let token = mint(ISSUER, "hostexec-wyrm2", &["hostexec-root-wyrm2"], FUTURE);
    let r = verify_operator_token(&token, &key(), ISSUER, "wyrm2", "agentydragon");
    assert!(matches!(r, Err(AuthError::NotAuthorized { .. })));
}

#[test]
fn rejects_wrong_host_audience() {
    // A token minted for rugged must not be accepted by wyrm2's hostexecd.
    let token = mint(ISSUER, "hostexec-rugged", &["hostexec-root-wyrm2"], FUTURE);
    let r = verify_operator_token(&token, &key(), ISSUER, "wyrm2", "root");
    assert!(matches!(r, Err(AuthError::Token(_))));
}

#[test]
fn rejects_wrong_issuer() {
    let token = mint(
        "https://evil.example/",
        "hostexec-wyrm2",
        &["hostexec-root-wyrm2"],
        FUTURE,
    );
    let r = verify_operator_token(&token, &key(), ISSUER, "wyrm2", "root");
    assert!(matches!(r, Err(AuthError::Token(_))));
}

#[test]
fn rejects_expired() {
    let token = mint(ISSUER, "hostexec-wyrm2", &["hostexec-root-wyrm2"], PAST);
    let r = verify_operator_token(&token, &key(), ISSUER, "wyrm2", "root");
    assert!(matches!(r, Err(AuthError::Token(_))));
}

#[test]
fn rejects_tampered_signature() {
    let token = mint(ISSUER, "hostexec-wyrm2", &["hostexec-root-wyrm2"], FUTURE);
    let tampered = format!(
        "{}{}",
        &token[..token.len() - 1],
        if token.ends_with('A') { "B" } else { "A" }
    );
    let r = verify_operator_token(&tampered, &key(), ISSUER, "wyrm2", "root");
    assert!(matches!(r, Err(AuthError::Token(_))));
}
