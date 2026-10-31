//! The analysis artifact: the program-wide (S2) and per-chunk (S3) analysis
//! output of a run, encoded independently of the process that computed it,
//! behind a header naming what it was computed from (plans/staged_pipeline.md).
//!
//! Layout: the header's byte length (`u32`, little-endian), the header as
//! named MessagePack, then the body as `analysis_codec` output. The header
//! reads without parsing any chunk, so a run stage can check it before it
//! re-parses the sources the body's contexts refer to.
//!
//! The artifact holds `CrossModulePurities` once. Each chunk's
//! `AnalysisInputs` derive from it and its chunk options; storing them
//! instead would repeat the program-wide census per chunk (1.77 GB on the
//! Claude CLI).

use std::collections::BTreeMap;

use analysis::facts::ChunkFactAnalysis;
use analysis_codec::ContextOrdinals;
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

use super::ExportReentryReport;
use super::cross_module::CrossModulePurities;

/// What an artifact was computed from; a reader recomputes it and refuses an
/// artifact whose header differs.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnalysisArtifactHeader {
    pub engine_sha256: String,
    pub projection_sha256: String,
    /// Parser-input SHA-256 of each analyzed chunk, by chunk name.
    pub chunk_sources: BTreeMap<String, String>,
}

#[derive(Serialize, Deserialize)]
pub struct AnalysisArtifactBody {
    pub(crate) cross_module: CrossModulePurities,
    pub(crate) export_reentry: BTreeMap<String, ExportReentryReport>,
    pub(crate) chunks: BTreeMap<String, ChunkFactAnalysis>,
}

/// `ordinals` numbers the contexts of every module the analysis read; a
/// reader passes the same modules, parsed in its own process.
pub fn write_artifact(
    header: &AnalysisArtifactHeader,
    body: &AnalysisArtifactBody,
    ordinals: &ContextOrdinals,
) -> Result<Vec<u8>> {
    let header = rmp_serde::to_vec_named(header)?;
    let body = analysis_codec::encode(body, ordinals)?;
    let mut bytes = Vec::with_capacity(4 + header.len() + body.len());
    bytes.extend_from_slice(&u32::try_from(header.len())?.to_le_bytes());
    bytes.extend_from_slice(&header);
    bytes.extend_from_slice(&body);
    Ok(bytes)
}

/// The header and the still-encoded body.
pub fn read_header(bytes: &[u8]) -> Result<(AnalysisArtifactHeader, &[u8])> {
    let (length, rest) = bytes
        .split_first_chunk::<4>()
        .context("analysis artifact is shorter than its header length")?;
    let length = usize::try_from(u32::from_le_bytes(*length))?;
    ensure!(
        rest.len() >= length,
        "analysis artifact header claims {length} bytes, {} remain",
        rest.len()
    );
    let (header, body) = rest.split_at(length);
    Ok((rmp_serde::from_slice(header)?, body))
}

pub fn read_body(body: &[u8], ordinals: &ContextOrdinals) -> Result<AnalysisArtifactBody> {
    analysis_codec::decode(body, ordinals)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_projection::AnalysisProjection;
    use crate::cross_module::{CrossModuleAnalysisOptions, collect_cross_module_imported_purities};
    use analysis::AnalysisInputs;
    use artifact::reports::{ChunkAnalysisReport, ChunkFileRecord};
    use artifact::{
        ArtifactIndexes, ChunkArtifact, ChunkBundle, ChunkMetadata, ChunkTable, FileMetadata,
        FileRole, JsChunk, JsFile, JsFileBody,
    };
    use program_analysis::analyze_program_shallow;
    use std::process::Command;
    use swc_common::{GLOBALS, Globals};

    /// Imports, a closure-local binding shadowing a top-level one, and a
    /// callback through an imported function, so the facts carry top-level,
    /// inner-scope and unresolved contexts.
    const CHUNKS: &[(&str, &str)] = &[
        (
            "static/app",
            "import { counter as shared, wrap } from '../peer/peer.js';\n\
             const counter = shared + 1;\n\
             function increment() { let counter = 1; return counter; }\n\
             export const wrapped = wrap(() => counter + increment());\n\
             window.app = wrapped;",
        ),
        (
            "static/peer",
            "export const counter = 41;\n\
             export function wrap(callback) { const inner = () => callback(); return inner; }",
        ),
    ];

    /// Parses `CHUNKS` in `parse_order` into retained ASTs, so whole-program
    /// analysis reads the same modules the test numbers.
    fn bundle(parse_order: &[usize]) -> ChunkBundle {
        let mut parsed = BTreeMap::new();
        for &index in parse_order {
            let (name, source) = CHUNKS[index];
            let entry = format!("{}.js", name.rsplit('/').next().expect("named chunk"));
            let module = js_ast::parse_js_module(&format!("{name}/{entry}"), source)
                .expect("fixture parses");
            parsed.insert(index, (entry, module));
        }
        let mut chunk_table = ChunkTable::default();
        let chunks = CHUNKS
            .iter()
            .enumerate()
            .map(|(index, (name, _))| {
                let (entry, module) = parsed.remove(&index).expect("every chunk parsed");
                let analysis = analyze_program_shallow(&module);
                let source_path = format!("{name}.js");
                ChunkArtifact {
                    chunk_id: chunk_table.intern((*name).to_string()),
                    js: JsChunk {
                        entry_file: entry.clone(),
                        files: vec![JsFile {
                            path: entry.clone(),
                            body: JsFileBody::Ast(module),
                            header_lines: Vec::new(),
                            binding_comments: BTreeMap::new(),
                            leading_item_comments: BTreeMap::new(),
                            metadata: FileMetadata {
                                chunk_id: (*name).to_string(),
                                chunk_file: entry.clone(),
                                role: FileRole::Entry,
                                source_path: source_path.clone(),
                            },
                        }],
                        metadata: ChunkMetadata {
                            source_path: source_path.clone(),
                        },
                    },
                    analysis: ChunkAnalysisReport {
                        chunk_id: (*name).to_string(),
                        source_path,
                        entry_file: entry.clone(),
                        counts: Default::default(),
                        files: vec![ChunkFileRecord {
                            file: entry,
                            role: FileRole::Entry,
                        }],
                        imports: analysis.imports,
                        export_aliases: analysis.export_aliases,
                        unresolved_exports: Vec::new(),
                        kept_top_level_declarations: Vec::new(),
                        runtime_facts: analysis.runtime_facts,
                    },
                }
            })
            .collect();
        ChunkBundle {
            chunks,
            chunk_table,
        }
    }

    fn modules(artifact: &ChunkBundle) -> impl Iterator<Item = &swc_ecma_ast::Module> {
        artifact.chunks.iter().map(|chunk| {
            &chunk
                .js
                .get_file(&chunk.js.entry_file)
                .and_then(JsFile::ast)
                .expect("retained entry AST")
                .module
        })
    }

    fn analyze(artifact: &ChunkBundle) -> AnalysisArtifactBody {
        let mut assertion = spec::ChunkExportPurity::default();
        assertion.pure_exports.insert("wrap".to_string());
        let assertions = BTreeMap::from([("static/peer".to_string(), assertion)]);
        let indexes = ArtifactIndexes::build(artifact).expect("build indexes");
        let no_options = BTreeMap::new();
        let host_behavior = spec::HostBehavior::default();
        let cross_module = collect_cross_module_imported_purities(
            artifact,
            &indexes,
            &AnalysisProjection::new(
                &no_options,
                &assertions,
                &host_behavior,
                BTreeMap::new(),
                &BTreeMap::new(),
            ),
            CrossModuleAnalysisOptions {
                graph_provenance: None,
                explicit_binding_destinations: &BTreeMap::new(),
                replacement_packages: &BTreeMap::new(),
            },
            None,
        )
        .expect("cross-module analysis succeeds");
        let chunks = artifact
            .chunks
            .iter()
            .zip(modules(artifact))
            .map(|(chunk, module)| {
                let name = artifact.chunk_table.name(chunk.chunk_id).to_string();
                let inputs = AnalysisInputs {
                    imported_purities: cross_module
                        .bindings
                        .get(&name)
                        .cloned()
                        .unwrap_or_default(),
                    ..AnalysisInputs::default()
                };
                let facts = analysis::facts::analyze_chunk(module, &inputs, None, |_| None);
                (name, facts)
            })
            .collect();
        AnalysisArtifactBody {
            cross_module,
            export_reentry: BTreeMap::new(),
            chunks,
        }
    }

    fn header() -> AnalysisArtifactHeader {
        AnalysisArtifactHeader {
            engine_sha256: "engine-digest-fixture".to_string(),
            projection_sha256: "projection-digest-fixture".to_string(),
            chunk_sources: CHUNKS
                .iter()
                .map(|(name, _)| (name.to_string(), format!("{name}-digest-fixture")))
                .collect(),
        }
    }

    /// The lessons-learned round trip: encode in one `Globals`, re-parse in a
    /// fresh one whose contexts are numbered differently, decode, and get
    /// exactly the facts a fresh analysis there computes.
    #[test]
    fn decodes_into_a_fresh_globals() {
        let (bytes, debug_first) = GLOBALS.set(&Globals::new(), || {
            let artifact = bundle(&[0, 1]);
            let body = analyze(&artifact);
            let ordinals = ContextOrdinals::new(modules(&artifact));
            (
                write_artifact(&header(), &body, &ordinals).expect("encode"),
                format!("{:?}", body.chunks),
            )
        });
        GLOBALS.set(&Globals::new(), || {
            // Parsing a module first and the chunks in reverse shifts every
            // context number away from the first `Globals`.
            js_ast::parse_js_module("shift.js", "let a = () => { let b; };").expect("parses");
            let artifact = bundle(&[1, 0]);
            let fresh = analyze(&artifact);
            let ordinals = ContextOrdinals::new(modules(&artifact));
            let (read, body) = read_header(&bytes).expect("header");
            assert_eq!(read, header());
            let decoded = read_body(body, &ordinals).expect("decode");
            let debug_fresh = format!("{:?}", fresh.chunks);
            assert_ne!(debug_fresh, debug_first, "contexts are renumbered");
            assert_eq!(format!("{:?}", decoded.chunks), debug_fresh);
            assert_eq!(
                write_artifact(&header(), &decoded, &ordinals).expect("re-encode"),
                write_artifact(&header(), &fresh, &ordinals).expect("encode fresh"),
            );
        });
    }

    const CHILD_OUTPUT: &str = "ANALYSIS_ARTIFACT_TEST_OUTPUT";
    const CHILD_REVERSE: &str = "ANALYSIS_ARTIFACT_TEST_REVERSE";

    /// Child half of `fresh_processes_write_identical_bytes`: analyzes the
    /// fixture and writes the artifact and its raw MessagePack encoding.
    #[test]
    #[ignore = "run by fresh_processes_write_identical_bytes"]
    fn write_fixture_artifact() {
        let output = std::path::PathBuf::from(std::env::var(CHILD_OUTPUT).expect(CHILD_OUTPUT));
        let order: &[usize] = if std::env::var_os(CHILD_REVERSE).is_some() {
            &[1, 0]
        } else {
            &[0, 1]
        };
        js_ast::with_swc_globals(|| {
            let artifact = bundle(order);
            let body = analyze(&artifact);
            let ordinals = ContextOrdinals::new(modules(&artifact));
            std::fs::write(
                output.with_extension("dba"),
                write_artifact(&header(), &body, &ordinals).expect("encode"),
            )
            .expect("write artifact");
            std::fs::write(
                output.with_extension("raw"),
                rmp_serde::to_vec_named(&body).expect("raw encode"),
            )
            .expect("write raw encoding");
        });
    }

    #[test]
    fn fresh_processes_write_identical_bytes() {
        let dir = std::path::PathBuf::from(std::env::var("TEST_TMPDIR").expect("TEST_TMPDIR"));
        let run = |name: &str, reverse: bool| {
            let output = dir.join(name);
            let mut command = Command::new(std::env::current_exe().expect("test binary"));
            command
                .args([
                    "--exact",
                    "analysis_artifact::tests::write_fixture_artifact",
                    "--ignored",
                ])
                .env(CHILD_OUTPUT, &output);
            if reverse {
                command.env(CHILD_REVERSE, "1");
            }
            let status = command.status().expect("run child test");
            assert!(status.success(), "child {name} failed");
            (
                std::fs::read(output.with_extension("dba")).expect("artifact"),
                std::fs::read(output.with_extension("raw")).expect("raw encoding"),
            )
        };
        let (first, first_raw) = run("forward", false);
        let (second, second_raw) = run("reverse", true);
        assert_ne!(first_raw, second_raw, "parse order changes raw contexts");
        assert_eq!(first, second);
    }
}
