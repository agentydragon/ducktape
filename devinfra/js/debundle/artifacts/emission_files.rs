//! Ownership of lowered files and their final output-path indexes.

use anyhow::Result;

use super::indexed_files::IndexedFiles;
use super::{ArtifactIndexes, ChunkBundle, SourceProgram};

/// Emit-stage ownership: finalized lowered/pass-through files and the
/// indexes matching their final paths. Transformations may rewrite bodies
/// but cannot silently change the emission set's indexed file layout.
pub struct EmissionFiles {
    indexed: IndexedFiles,
}

impl EmissionFiles {
    pub fn new(files: ChunkBundle) -> Result<Self> {
        Ok(Self {
            indexed: IndexedFiles::new(files)?,
        })
    }

    pub fn from_prepared(program: SourceProgram) -> Self {
        Self {
            indexed: program.indexed,
        }
    }

    pub fn files(&self) -> &ChunkBundle {
        self.indexed.artifact()
    }

    pub fn indexes(&self) -> &ArtifactIndexes {
        self.indexed.indexes()
    }

    pub fn rewrite_bodies<T>(
        self,
        rewrite: impl FnOnce(&mut ChunkBundle, &ArtifactIndexes) -> Result<T>,
    ) -> Result<(Self, T)> {
        let (indexed, value) = self.indexed.update_file_bodies(rewrite)?;
        Ok((Self { indexed }, value))
    }
}
