//! Saved apply inputs, with compiled spec conversions shared with analysis.
use super::{ApplyPath, ApplyProjection, TransformSpecSource};
use anyhow::{Context, Result, ensure};
use apply_projection_wire_proto::debundle::apply as wire;
use prost::Message;
use spec_wire::{required, unique_set};
use spec_wire_proto::debundle::spec::Unit;

impl ApplyProjection {
    pub(super) fn encode(&self) -> Vec<u8> {
        self.to_proto().encode_to_vec()
    }
    pub(super) fn decode(bytes: &[u8]) -> Result<Self> {
        let value = wire::ApplyProjection::decode(bytes)?;
        ensure!(
            value.format_version == super::APPLY_PROJECTION_FORMAT_VERSION,
            "unsupported apply projection format version {}",
            value.format_version
        );
        Self::from_proto(value)
    }
    fn to_proto(&self) -> wire::ApplyProjection {
        wire::ApplyProjection {
            format_version: self.format_version,
            source: Some(self.source.to_proto()),
            spec: Some(spec_wire::encode_transform_spec(&self.spec)),
            module_roots_by_chunk: self
                .module_roots_by_chunk
                .iter()
                .map(|(key, paths)| {
                    (
                        key.clone(),
                        wire::ModuleRoots {
                            paths: paths
                                .iter()
                                .map(|path| spec_wire::encode_path(path))
                                .collect(),
                        },
                    )
                })
                .collect(),
            rooted_paths: self.rooted_paths.iter().map(ApplyPath::to_proto).collect(),
        }
    }
    fn from_proto(v: wire::ApplyProjection) -> Result<Self> {
        Ok(Self {
            format_version: v.format_version,
            source: TransformSpecSource::from_proto(
                v.source.context("missing apply spec source")?,
            )?,
            spec: spec_wire::decode_transform_spec(required(v.spec)?)?,
            module_roots_by_chunk: v
                .module_roots_by_chunk
                .into_iter()
                .map(|(key, value)| {
                    (
                        key,
                        value
                            .paths
                            .into_iter()
                            .map(spec_wire::decode_path)
                            .collect(),
                    )
                })
                .collect(),
            rooted_paths: unique_set(v.rooted_paths.into_iter().map(ApplyPath::from_proto))?,
        })
    }
}
impl TransformSpecSource {
    fn to_proto(&self) -> wire::TransformSpecSource {
        use wire::transform_spec_source::Value;
        wire::TransformSpecSource {
            value: Some(match self {
                Self::Flat { path } => Value::FlatPath(spec_wire::encode_path(path)),
                Self::Tree(tree) => Value::Tree(wire::CompileSpecTreeOptions {
                    config_path: spec_wire::encode_path(&tree.config_path),
                    modules_root: spec_wire::encode_path(&tree.modules_root),
                    vendor_marks_path: spec_wire::encode_path(&tree.vendor_marks_path),
                    source_root: tree.source_root.as_deref().map(spec_wire::encode_path),
                    out_root: spec_wire::encode_path(&tree.out_root),
                }),
            }),
        }
    }
    fn from_proto(v: wire::TransformSpecSource) -> Result<Self> {
        use wire::transform_spec_source::Value;
        Ok(
            match v.value.context("missing apply spec source variant")? {
                Value::FlatPath(path) => Self::Flat {
                    path: spec_wire::decode_path(path),
                },
                Value::Tree(v) => Self::Tree(spec_tree::CompileSpecTreeOptions {
                    config_path: spec_wire::decode_path(v.config_path),
                    modules_root: spec_wire::decode_path(v.modules_root),
                    vendor_marks_path: spec_wire::decode_path(v.vendor_marks_path),
                    source_root: v.source_root.map(spec_wire::decode_path),
                    out_root: spec_wire::decode_path(v.out_root),
                }),
            },
        )
    }
}
impl ApplyPath {
    fn to_proto(&self) -> wire::ApplyPath {
        use wire::apply_path::Value;
        wire::ApplyPath {
            value: Some(match self {
                Self::InputRoot => Value::InputRoot(Unit {}),
                Self::JsList => Value::JsList(Unit {}),
                Self::EmbeddedLoaders => Value::EmbeddedLoaders(Unit {}),
                Self::VendorBundle(chunk) => Value::VendorBundle(chunk.clone()),
                Self::VendorManifest => Value::VendorManifest(Unit {}),
                Self::VendorWrappers => Value::VendorWrappers(Unit {}),
                Self::Reports => Value::Reports(Unit {}),
                Self::JsOutput => Value::JsOutput(Unit {}),
                Self::BrowserAssets => Value::BrowserAssets(Unit {}),
                Self::BrowserOutput => Value::BrowserOutput(Unit {}),
                Self::BrowserSnapshot => Value::BrowserSnapshot(Unit {}),
            }),
        }
    }
    fn from_proto(v: wire::ApplyPath) -> Result<Self> {
        use wire::apply_path::Value;
        Ok(match v.value.context("missing apply path variant")? {
            Value::InputRoot(_) => Self::InputRoot,
            Value::JsList(_) => Self::JsList,
            Value::EmbeddedLoaders(_) => Self::EmbeddedLoaders,
            Value::VendorBundle(chunk) => Self::VendorBundle(chunk),
            Value::VendorManifest(_) => Self::VendorManifest,
            Value::VendorWrappers(_) => Self::VendorWrappers,
            Value::Reports(_) => Self::Reports,
            Value::JsOutput(_) => Self::JsOutput,
            Value::BrowserAssets(_) => Self::BrowserAssets,
            Value::BrowserOutput(_) => Self::BrowserOutput,
            Value::BrowserSnapshot(_) => Self::BrowserSnapshot,
        })
    }
}
#[cfg(test)]
#[path = "apply_projection_codec_tests.rs"]
mod tests;
