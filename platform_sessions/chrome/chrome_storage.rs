// Read Chromium's local-storage LevelDB through an in-memory copy. Values go
// only to the calling process; Chrome's live database is never opened for writing.
// CCL's Python Chromium reader exposes historical records and tombstones, rather
// than the current LevelDB view, and scans files outside the active manifest.
// The in-memory LevelDB snapshot resolves these; copying files can still race Chrome.
use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::rc::Rc;

use anyhow::{Context, Result, bail};
use rusty_leveldb::{DB, LdbIterator, MemEnv, Options, env::Env};

fn decode_value(bytes: &[u8]) -> Option<String> {
    match bytes.split_first()? {
        (1, content) => Some(content.iter().map(|byte| char::from(*byte)).collect()),
        (0, content) if content.len() % 2 == 0 => String::from_utf16(
            &content
                .as_chunks::<2>()
                .0
                .iter()
                .map(|chunk| u16::from_le_bytes([chunk[0], chunk[1]]))
                .collect::<Vec<_>>(),
        )
        .ok(),
        _ => None,
    }
}

fn read_entry(
    key: &[u8],
    value: &[u8],
    origin: &[u8],
    keys: &[String],
    prefixes: &[String],
) -> Result<Option<(String, String)>> {
    if !key.starts_with(origin) {
        return Ok(None);
    }
    let key = decode_value(&key[origin.len()..]).unwrap_or_default();
    if !keys.contains(&key) && !prefixes.iter().any(|prefix| key.starts_with(prefix)) {
        return Ok(None);
    }
    let value = decode_value(value).context("unsupported Chrome local-storage encoding")?;
    Ok(Some((key, value)))
}

fn open_snapshot(source: &Path) -> Result<DB> {
    let env = MemEnv::new();
    let root = Path::new("/chrome-storage");
    env.mkdir(root)
        .context("could not prepare in-memory Chrome snapshot")?;
    for entry in std::fs::read_dir(source).context("could not read Chrome local storage")? {
        let entry = entry.context("could not inspect Chrome local storage")?;
        if !entry.file_type()?.is_file() || entry.file_name() == "LOCK" {
            continue;
        }
        let bytes = std::fs::read(entry.path()).context("could not copy Chrome local storage")?;
        let mut output = env.open_writable_file(&root.join(entry.file_name()))?;
        output.write_all(&bytes)?;
    }
    DB::open(
        root,
        Options {
            env: Rc::new(Box::new(env)),
            create_if_missing: false,
            ..Options::default()
        },
    )
    .context("could not open in-memory Chrome local storage")
}

fn main() -> Result<()> {
    let mut args = std::env::args_os().skip(1);
    let path = PathBuf::from(args.next().context("expected Chrome LevelDB path")?);
    let origin = args
        .next()
        .context("expected storage origin")?
        .into_string()
        .map_err(|_| anyhow::anyhow!("invalid storage origin"))?;
    let mut keys = Vec::new();
    let mut prefixes = Vec::new();
    while let Some(flag) = args.next() {
        let value = args
            .next()
            .context("missing selector value")?
            .into_string()
            .map_err(|_| anyhow::anyhow!("invalid selector"))?;
        match flag.to_str() {
            Some("--key") => keys.push(value),
            Some("--prefix") => prefixes.push(value),
            _ => bail!("unknown selector"),
        }
    }
    if keys.is_empty() && prefixes.is_empty() {
        bail!("at least one storage selector is required");
    }
    let origin = format!("_{origin}\0").into_bytes();
    let mut db = open_snapshot(&path)?;
    let mut iter = db
        .new_iter()
        .context("could not read copied Chrome local storage")?;
    let mut values = BTreeMap::new();
    while iter.advance() {
        let (key, value) = iter.current().context("invalid LevelDB record")?;
        if let Some((key, value)) = read_entry(&key, &value, &origin, &keys, &prefixes)? {
            values.insert(key, value);
        }
    }
    if values.is_empty() {
        bail!("no matching Chrome local-storage entries found");
    }
    println!("{}", serde_json::to_string(&values)?);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const ORIGIN: &[u8] = b"_https://example.com\0";
    const SDK_KEY: &str = "auth:onboard";
    const LEGACY_KEY_PREFIX: &str = "legacy:auth:";

    #[test]
    fn selects_platform_auth_storage_and_decodes_both_string_formats() {
        let mut key = ORIGIN.to_vec();
        key.push(1);
        key.extend_from_slice(format!("{LEGACY_KEY_PREFIX}openid").as_bytes());
        let value = [1u8]
            .into_iter()
            .chain(br#"{"body":{}}"#.iter().copied())
            .collect::<Vec<_>>();
        let (_, result) = read_entry(
            &key,
            &value,
            ORIGIN,
            &[SDK_KEY.into()],
            &[LEGACY_KEY_PREFIX.into()],
        )
        .unwrap()
        .unwrap();
        assert_eq!(result, r#"{"body":{}}"#);
        assert!(
            read_entry(
                b"_https://other.example\0\x01key",
                &value,
                ORIGIN,
                &[SDK_KEY.into()],
                &[LEGACY_KEY_PREFIX.into()]
            )
            .unwrap()
            .is_none()
        );
        let mut sdk_key = ORIGIN.to_vec();
        sdk_key.push(1);
        sdk_key.extend_from_slice(SDK_KEY.as_bytes());
        assert_eq!(
            read_entry(
                &sdk_key,
                &value,
                ORIGIN,
                &[SDK_KEY.into()],
                &[LEGACY_KEY_PREFIX.into()]
            )
            .unwrap()
            .unwrap()
            .0,
            SDK_KEY
        );
        assert_eq!(decode_value(&[0, b'A', 0, b'B', 0]), Some("AB".into()));
    }

    #[test]
    fn iterates_a_real_copied_leveldb() {
        let root = std::path::PathBuf::from(std::env::var_os("TEST_TMPDIR").unwrap());
        let path = root.join("chrome-local-storage");
        let mut db = DB::open(&path, Options::default()).unwrap();
        let mut key = ORIGIN.to_vec();
        key.push(1);
        key.extend_from_slice(format!("{LEGACY_KEY_PREFIX}openid").as_bytes());
        db.put(&key, b"\x01{\"body\":{}}").unwrap();
        db.put(b"_https://other.example\0\x01key", b"\x01private")
            .unwrap();
        let mut unrelated = ORIGIN.to_vec();
        unrelated.extend_from_slice(b"\x01unrelated");
        db.put(&unrelated, b"\x01other-secret").unwrap();
        drop(db);
        let mut db = open_snapshot(&path).unwrap();
        let mut iter = db.new_iter().unwrap();
        let mut found = Vec::new();
        while iter.advance() {
            let (key, value) = iter.current().unwrap();
            if let Some(entry) = read_entry(
                &key,
                &value,
                ORIGIN,
                &[SDK_KEY.into()],
                &[LEGACY_KEY_PREFIX.into()],
            )
            .unwrap()
            {
                found.push(entry);
            }
        }
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].1, r#"{"body":{}}"#);
    }

    #[test]
    fn cli_only_exports_requested_origin_and_keys() {
        let root = PathBuf::from(std::env::var_os("TEST_TMPDIR").unwrap());
        let path = root.join("cli-storage");
        let mut db = DB::open(&path, Options::default()).unwrap();
        db.put(b"_https://example.com\0\x01session", b"\x01selected")
            .unwrap();
        db.put(b"_https://example.com\0\x01unrelated", b"\x01private")
            .unwrap();
        db.put(b"_https://other.example\0\x01session", b"\x01private")
            .unwrap();
        drop(db);
        let binary = PathBuf::from(std::env::var_os("TEST_SRCDIR").unwrap())
            .join("_main/platform_sessions/chrome/chrome_storage");
        let output = std::process::Command::new(binary)
            .arg(path)
            .args(["https://example.com", "--key", "session"])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let values: BTreeMap<String, String> = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(
            values,
            BTreeMap::from([("session".into(), "selected".into())])
        );
    }
}
