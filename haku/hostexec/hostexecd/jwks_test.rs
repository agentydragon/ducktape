//! JWKS indexing tests. `testdata/jwks.json` is generated from the same test RSA key the token
//! tests sign with, so the key it yields actually verifies a token minted by that key — proving
//! the full JWKS → `DecodingKey` → verify path, not just that indexing does not panic.

use authentik::verify_operator_token;
use jsonwebtoken::jwk::JwkSet;
use jsonwebtoken::{Algorithm, EncodingKey, Header, encode};
use jwks::{JwksError, index};
use serde::Serialize;

const PRIVATE_PEM: &[u8] = include_bytes!("testdata/rsa_test_private.pem");
const JWKS_JSON: &str = include_str!("testdata/jwks.json");
const ISSUER: &str = "https://auth.allegedly.works/application/o/hostexec-wyrm2/";

#[derive(Serialize)]
struct TestClaims {
    iss: String,
    aud: String,
    sub: String,
    groups: Vec<String>,
    exp: u64,
}

#[test]
fn index_yields_a_key_that_verifies_a_token() {
    let set: JwkSet = serde_json::from_str(JWKS_JSON).unwrap();
    let keys = index(&set).unwrap();
    let key = keys.get("hostexec-test").expect("kid present");

    let claims = TestClaims {
        iss: ISSUER.to_string(),
        aud: "hostexec-wyrm2".to_string(),
        sub: "operator-agentydragon".to_string(),
        groups: vec!["hostexec-root-wyrm2".to_string()],
        exp: 4102444800,
    };
    let mut header = Header::new(Algorithm::RS256);
    header.kid = Some("hostexec-test".to_string());
    let token = encode(
        &header,
        &claims,
        &EncodingKey::from_rsa_pem(PRIVATE_PEM).unwrap(),
    )
    .unwrap();

    let op = verify_operator_token(&token, key, ISSUER, "wyrm2", "root").unwrap();
    assert_eq!(op.subject, "operator-agentydragon");
}

#[test]
fn index_rejects_entry_without_kid() {
    let set: JwkSet = serde_json::from_str(
        r#"{"keys":[{"kty":"RSA","use":"sig","alg":"RS256","n":"AQAB","e":"AQAB"}]}"#,
    )
    .unwrap();
    assert!(matches!(index(&set), Err(JwksError::MissingKid)));
}

#[test]
fn unknown_kid_is_absent_from_the_index() {
    let set: JwkSet = serde_json::from_str(JWKS_JSON).unwrap();
    let keys = index(&set).unwrap();
    assert!(!keys.contains_key("some-other-kid"));
}
