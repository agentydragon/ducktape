use std::path::Path;

pub(crate) fn resolve_dep(source_path: &str, spec: &str) -> Option<String> {
    if !(spec.starts_with("./") || spec.starts_with("../")) {
        return None;
    }
    let parent = Path::new(source_path).parent()?;
    let mut joined = parent.join(spec);
    if joined.extension().is_none() {
        joined.set_extension("js");
    }
    Some(joined.to_string_lossy().replace('\\', "/"))
}
