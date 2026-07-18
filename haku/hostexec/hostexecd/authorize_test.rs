//! Authorization-composition tests: a valid request yields the run_as credentials; a reused token,
//! an unauthorized token, and a nonexistent run_as user each fail with their distinct variant.
//!
//! Self-contained: mints RS256 tokens with the fixed test RSA keypair (`testdata/`) and resolves
//! real accounts (`root`) from the host passwd database, exactly as `hostexecd` will.

use authorize::{AuthorizeError, Authorized, authorize};
use jsonwebtoken::{Algorithm, DecodingKey, EncodingKey, Header, encode};
use replay::ReplayStore;
use serde::Serialize;

const PRIVATE_PEM: &[u8] = include_bytes!("testdata/rsa_test_private.pem");
const PUBLIC_PEM: &[u8] = include_bytes!("testdata/rsa_test_public.pem");
const ISSUER: &str = "https://auth.allegedly.works/application/o/hostexec-wyrm2/";
const SUBJECT: &str = "operator-agentydragon";
const FUTURE: u64 = 4102444800; // 2100-01-01
const NOW: u64 = 1700000000; // 2023, well before FUTURE — so the replay entry is live.

#[derive(Serialize)]
struct TestClaims {
    iss: String,
    aud: String,
    sub: String,
    groups: Vec<String>,
    exp: u64,
}

fn mint(groups: &[&str]) -> String {
    let claims = TestClaims {
        iss: ISSUER.to_string(),
        aud: "hostexec-wyrm2".to_string(),
        sub: SUBJECT.to_string(),
        groups: groups.iter().map(|g| g.to_string()).collect(),
        exp: FUTURE,
    };
    let key = EncodingKey::from_rsa_pem(PRIVATE_PEM).unwrap();
    encode(&Header::new(Algorithm::RS256), &claims, &key).unwrap()
}

fn key() -> DecodingKey {
    DecodingKey::from_rsa_pem(PUBLIC_PEM).unwrap()
}

#[test]
fn authorizes_valid_request() {
    let token = mint(&["hostexec-root-wyrm2"]);
    let replay = ReplayStore::new();
    let Authorized {
        subject,
        credentials,
    } = authorize(&token, &key(), ISSUER, "wyrm2", "root", &replay, NOW).unwrap();
    assert_eq!(subject, SUBJECT);
    assert_eq!(credentials.uid, 0);
    assert_eq!(credentials.gid, 0);
}

#[test]
fn rejects_reused_token() {
    let token = mint(&["hostexec-root-wyrm2"]);
    let replay = ReplayStore::new();
    authorize(&token, &key(), ISSUER, "wyrm2", "root", &replay, NOW).unwrap();
    let second = authorize(&token, &key(), ISSUER, "wyrm2", "root", &replay, NOW);
    assert!(matches!(second, Err(AuthorizeError::Replay(_))));
}

#[test]
fn rejects_unauthorized_token() {
    // Token carries no group authorizing root on wyrm2.
    let token = mint(&["hostexec-agentydragon-wyrm2"]);
    let replay = ReplayStore::new();
    let result = authorize(&token, &key(), ISSUER, "wyrm2", "root", &replay, NOW);
    assert!(matches!(result, Err(AuthorizeError::Auth(_))));
}

#[test]
fn rejects_nonexistent_run_as_user() {
    // The group authorizes the (fictional) user, so verification passes, but passwd has no such
    // account — the request must fail loudly rather than run as anyone.
    let ghost = "nonexistent_hostexec_user_zzz";
    let token = mint(&[&format!("hostexec-{ghost}-wyrm2")]);
    let replay = ReplayStore::new();
    let result = authorize(&token, &key(), ISSUER, "wyrm2", ghost, &replay, NOW);
    assert!(matches!(result, Err(AuthorizeError::NoSuchUser(_))));
}

#[test]
fn nonexistent_user_does_not_burn_the_token() {
    // User-resolution failure precedes the single-use claim, so the same token can be presented
    // again once the account exists. (Here we just re-run and still get NoSuchUser, proving the
    // first attempt did not consume the replay slot.)
    let ghost = "nonexistent_hostexec_user_zzz";
    let token = mint(&[&format!("hostexec-{ghost}-wyrm2")]);
    let replay = ReplayStore::new();
    let first = authorize(&token, &key(), ISSUER, "wyrm2", ghost, &replay, NOW);
    assert!(matches!(first, Err(AuthorizeError::NoSuchUser(_))));
    let second = authorize(&token, &key(), ISSUER, "wyrm2", ghost, &replay, NOW);
    assert!(matches!(second, Err(AuthorizeError::NoSuchUser(_))));
}
