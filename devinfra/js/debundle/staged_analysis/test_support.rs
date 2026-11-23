//! Chunk bundles and program summaries for the `staged_analysis` unit tests.

use std::collections::BTreeMap;

use analysis::diagnostics::Diagnostics;
use analysis_projection::{AnalysisProjection, AnalysisSourceSites};
use artifact::reports::{ChunkAnalysisReport, ChunkFileRecord};
use artifact::{
    ArtifactIndexes, ChunkArtifact, ChunkBundle, ChunkMetadata, FileMetadata, FileRole, JsChunk,
    JsFile, JsFileBody,
};
use program_analysis::analyze_program_shallow;
use source_identity::ChunkTable;
use spec::ChunkExportPurity;

use crate::program_summary::{ProgramSummary, ProgramSummaryOptions, collect_program_summary};

/// Entry-only chunks with their parsed ASTs retained, parsed in listed order.
pub(crate) fn test_bundle(chunks: &[(&str, &str)]) -> ChunkBundle {
    test_bundle_parsed_in(chunks, &(0..chunks.len()).collect::<Vec<_>>())
}

/// [`test_bundle`] with the sources parsed in `parse_order` (indexes into
/// `chunks`) before the chunks are assembled in listed order. The retained ASTs
/// make whole-program analysis read the same modules a test numbers.
pub(crate) fn test_bundle_parsed_in(chunks: &[(&str, &str)], parse_order: &[usize]) -> ChunkBundle {
    let mut parsed = BTreeMap::new();
    for &index in parse_order {
        let (name, source) = chunks[index];
        let entry = format!(
            "{}.js",
            name.rsplit('/')
                .next()
                .expect("test chunk names are non-empty")
        );
        let module = js_ast::parse_js_module(&format!("{name}/{entry}"), source)
            .expect("test source parses");
        parsed.insert(index, (entry, module));
    }
    let mut chunk_table = ChunkTable::default();
    let artifacts = chunks
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
        chunks: artifacts,
        chunk_table,
    }
}

/// The program summary of `artifact` under an empty projection that carries
/// only `export_purity_assertions`.
pub(crate) fn collect_test_program_summary(
    artifact: &ChunkBundle,
    export_purity_assertions: &BTreeMap<String, ChunkExportPurity>,
) -> ProgramSummary {
    let indexes = ArtifactIndexes::build(artifact).expect("build indexes");
    let no_options = BTreeMap::new();
    let host_behavior = spec::HostBehavior::default();
    collect_program_summary(
        artifact,
        &indexes,
        &AnalysisProjection::new(
            &no_options,
            export_purity_assertions,
            &host_behavior,
            None,
            BTreeMap::new(),
            AnalysisSourceSites::default(),
            BTreeMap::new(),
        )
        .expect("empty projection is valid"),
        ProgramSummaryOptions {
            graph_provenance: None,
            vendor_plan: None,
            replacement_packages: &BTreeMap::new(),
        },
        None,
        &Diagnostics::default(),
    )
    .expect("program summary analysis succeeds")
}
