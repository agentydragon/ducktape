//! File-backed cases run through the existing CLI, runtime and replay support.

mod case;
mod run;
#[cfg(test)]
mod tests;

use case::Expectation;
use runfiles::{Runfiles, rlocation};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Deserialize)]
struct ManifestFile {
    path: PathBuf,
    runfile: String,
}

#[derive(Clone)]
struct InputFile {
    relative: PathBuf,
    source: PathBuf,
}

pub struct Case {
    name: String,
    spec: PathBuf,
    inputs: Vec<InputFile>,
    expectation: Expectation,
}

impl Case {
    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn run(&self) {
        run::run(self);
    }
}

pub fn cases() -> Vec<Case> {
    let runfiles = Runfiles::create().expect("create corpus runfiles");
    let manifest = std::env::var("DEBUNDLE_TEST_CORPUS_MANIFEST").expect("corpus manifest runfile");
    let manifest = rlocation!(runfiles, &manifest).expect("resolve corpus manifest");
    let files: Vec<ManifestFile> =
        serde_json::from_slice(&fs::read(manifest).expect("read corpus manifest"))
            .expect("parse corpus manifest");
    let mut cases = Vec::new();
    for expectation_file in files
        .iter()
        .filter(|file| file.path.file_name().unwrap() == "expect.yaml")
    {
        let directory = expectation_file.path.parent().unwrap();
        let path = rlocation!(runfiles, &expectation_file.runfile).expect("resolve expectations");
        let expectations: BTreeMap<PathBuf, Expectation> =
            serde_yaml::from_slice(&fs::read(path).expect("read expectations"))
                .expect("parse expectation map");
        assert!(
            !expectations.is_empty(),
            "empty case: {}",
            directory.display()
        );
        let inputs: Vec<InputFile> = files
            .iter()
            .filter_map(|file| {
                let relative = file.path.strip_prefix(directory).ok()?.to_path_buf();
                Some(InputFile {
                    relative,
                    source: rlocation!(runfiles, &file.runfile).expect("resolve corpus input"),
                })
            })
            .collect();
        for (spec, expectation) in expectations {
            assert!(
                inputs.iter().any(|file| file.relative == spec),
                "missing spec {}",
                spec.display()
            );
            let name = directory.join(spec.file_stem().unwrap());
            cases.push(Case {
                name: name.to_str().expect("UTF-8 corpus name").to_owned(),
                spec,
                inputs: inputs.clone(),
                expectation,
            });
        }
    }
    assert!(!cases.is_empty(), "corpus has no cases");
    cases
}

fn strings(values: &[String]) -> Vec<&str> {
    values.iter().map(String::as_str).collect()
}

fn relative(path: &Path) {
    assert!(
        !path.as_os_str().is_empty()
            && path
                .components()
                .all(|part| matches!(part, std::path::Component::Normal(_))),
        "expected a case-relative path: {}",
        path.display()
    );
}
