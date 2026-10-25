//! Prepared source files and the indexes used by resolution and analysis.

use anyhow::Result;

use super::indexed_files::IndexedFiles;
use super::{ArtifactIndexes, ChunkBundle};

pub struct SourceProgram {
    pub(super) indexed: IndexedFiles,
}

impl SourceProgram {
    pub fn new(artifact: ChunkBundle) -> Result<Self> {
        Ok(Self {
            indexed: IndexedFiles::new(artifact)?,
        })
    }

    pub fn artifact(&self) -> &ChunkBundle {
        self.indexed.artifact()
    }

    pub fn indexes(&self) -> &ArtifactIndexes {
        self.indexed.indexes()
    }

    pub fn into_artifact(self) -> ChunkBundle {
        self.indexed.into_artifact()
    }
}
