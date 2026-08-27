// Extract a Firebase refresh token from a local Tana Desktop install's
// IndexedDB so the cluster Tana-MCP deployment can sign itself in without
// a noVNC operator session.
//
// Reads the well-known Firebase JS SDK persistence key
//   firebase:authUser:<API_KEY>:[DEFAULT]
// out of Chromium's leveldb-backed IndexedDB, decodes the V8 structured-clone
// payload, and prints `value.stsTokenManager.refreshToken` to stdout. Pipe
// the output straight into `sops -e` (see ../README.md). The token is enough
// for an in-cluster sidecar to keep minting fresh Firebase custom tokens and
// re-seed the desktop container's session.

use std::path::PathBuf;
use std::rc::Rc;

use anyhow::{Context, Result, anyhow, bail};
use rusty_leveldb::{DB, LdbIterator, Options};
use v8_valueserializer::{Heap, HeapValue, ParseError, PropertyKey, Value, ValueDeserializer};

// Chromium IndexedDB writes leveldb files under a custom comparator name
// (`idb_cmp1`). Its sort order matters for IDB internals; we scan every
// record, so bytewise comparison is enough.
#[derive(Clone, Default)]
struct IdbCmp1;

impl rusty_leveldb::Cmp for IdbCmp1 {
    fn cmp(&self, a: &[u8], b: &[u8]) -> std::cmp::Ordering {
        a.cmp(b)
    }
    fn id(&self) -> &'static str {
        "idb_cmp1"
    }
    fn find_shortest_sep(&self, a: &[u8], b: &[u8]) -> Vec<u8> {
        rusty_leveldb::DefaultCmp.find_shortest_sep(a, b)
    }
    fn find_short_succ(&self, a: &[u8]) -> Vec<u8> {
        rusty_leveldb::DefaultCmp.find_short_succ(a)
    }
}

fn main() -> Result<()> {
    let path: PathBuf = std::env::args()
        .nth(1)
        .ok_or_else(|| {
            anyhow!(
                "usage: tana_firebase_session_extractor <idb-leveldb-dir>\n\
                 e.g. ~/.config/tana-outliner/IndexedDB/https_app.tana.inc_0.indexeddb.leveldb"
            )
        })?
        .into();

    let opts = Options {
        cmp: Rc::new(Box::new(IdbCmp1)),
        create_if_missing: false,
        ..Options::default()
    };
    let mut db = DB::open(&path, opts).with_context(|| format!("open {path:?}"))?;

    // Firebase JS SDK persists the User as
    //   { fbase_key: "firebase:authUser:<API_KEY>:[DEFAULT]", value: <User> }
    // inside an IDB record. We don't bother reconstructing the IDB key
    // envelope; we just find the right record by scanning values for the
    // marker string. V8 picks OneByteString encoding for ASCII property
    // values, so the key string appears verbatim in the serialized bytes.
    let needle_ascii = b"firebase:authUser";
    let needle_u16 = encode_utf16_le("firebase:authUser");

    let mut iter = db.new_iter().context("new iterator")?;
    let mut hit: Option<Vec<u8>> = None;
    while iter.advance() {
        let (_k, value) = iter.current().expect("advance() returned true");
        if contains(&value, needle_ascii) || contains(&value, &needle_u16) {
            hit = Some(value.to_vec());
            break;
        }
    }
    let value = hit.ok_or_else(|| anyhow!("no firebase:authUser record found in {path:?}"))?;

    // The IDB value envelope wraps the V8 structured-clone blob with a small
    // header (object-store data version, optional wrapped-value flag). The V8
    // SSV payload itself starts with the Version tag 0xFF. Try every 0xFF in
    // the value buffer until V8 can parse from there.
    let (root, heap) = find_v8_payload(&value)?;

    let refresh_token =
        lookup_string_path(&root, &heap, &["value", "stsTokenManager", "refreshToken"])?;
    println!("{refresh_token}");
    Ok(())
}

fn find_v8_payload(value: &[u8]) -> Result<(Value, Heap)> {
    let mut last_err: Option<ParseError> = None;
    for (i, &b) in value.iter().enumerate() {
        if b != 0xff {
            continue;
        }
        let de = ValueDeserializer::default();
        match de.read(&value[i..]) {
            Ok(out) => return Ok(out),
            Err(e) => last_err = Some(e),
        }
    }
    bail!("no V8 SSV blob in value (last error at any 0xFF: {last_err:?})")
}

fn encode_utf16_le(s: &str) -> Vec<u8> {
    s.encode_utf16().flat_map(|c| c.to_le_bytes()).collect()
}

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

fn lookup_string_path(root: &Value, heap: &Heap, path: &[&str]) -> Result<String> {
    let mut cur = root.clone();
    for (i, segment) in path.iter().enumerate() {
        cur = lookup_one(&cur, heap, segment).with_context(|| format!("step {i} {segment:?}"))?;
    }
    match cur {
        Value::String(s) => Ok(s.to_string().into_owned()),
        other => bail!("expected string at end of path, got {other:?}"),
    }
}

fn lookup_one(parent: &Value, heap: &Heap, name: &str) -> Result<Value> {
    let r = match parent {
        Value::HeapReference(r) => *r,
        _ => bail!("not a heap reference: {parent:?}"),
    };
    let hv = r.open(heap);
    let entries: &Vec<(PropertyKey, Value)> = match hv {
        HeapValue::Object(o) => &o.properties,
        _ => bail!("not an object: {hv:?}"),
    };
    for (k, v) in entries {
        let k_str: std::borrow::Cow<'_, str> = match k {
            PropertyKey::String(s) => s.to_string(),
            PropertyKey::I32(n) => std::borrow::Cow::Owned(n.to_string()),
            PropertyKey::U32(n) => std::borrow::Cow::Owned(n.to_string()),
            PropertyKey::Double(d) => std::borrow::Cow::Owned(d.to_string()),
        };
        if k_str == name {
            return Ok(v.clone());
        }
    }
    bail!(
        "property {name:?} not in object; keys={:?}",
        entries.iter().map(|(k, _)| k).collect::<Vec<_>>()
    )
}
