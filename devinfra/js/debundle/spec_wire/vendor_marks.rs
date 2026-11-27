use super::{decode_map, decode_path, encode_map, encode_path, required};
use anyhow::{Context, Result};
use spec_wire_proto::debundle::spec as pb;

pub fn encode_vendor_mark(value: &spec::vendor_marks::VendorMark) -> pb::VendorMark {
    pb::VendorMark {
        identity: Some(value.identity.clone()),
        level: Some(encode_vendor_level(&value.level)),
    }
}

pub fn decode_vendor_mark(v: pb::VendorMark) -> Result<spec::vendor_marks::VendorMark> {
    Ok(spec::vendor_marks::VendorMark {
        identity: required(v.identity)?,
        level: decode_vendor_level(required(v.level)?)?,
    })
}

pub fn encode_vendor_level(value: &spec::vendor_marks::VendorLevel) -> pb::VendorLevel {
    use pb::vendor_level::Value;
    pb::VendorLevel {
        value: Some(match value {
            spec::vendor_marks::VendorLevel::Swap(v) => Value::Swap(encode_swap_mark(v)),
            spec::vendor_marks::VendorLevel::PartialSwap(v) => {
                Value::PartialSwap(encode_partial_swap_mark(v))
            }
            spec::vendor_marks::VendorLevel::BundledPartialSwap(v) => {
                Value::BundledPartialSwap(encode_bundled_partial_swap_mark(v))
            }
        }),
    }
}

pub fn decode_vendor_level(v: pb::VendorLevel) -> Result<spec::vendor_marks::VendorLevel> {
    use pb::vendor_level::Value;
    Ok(match v.value.context("missing vendor level")? {
        Value::Swap(v) => spec::vendor_marks::VendorLevel::Swap(decode_swap_mark(v)?),
        Value::PartialSwap(v) => {
            spec::vendor_marks::VendorLevel::PartialSwap(decode_partial_swap_mark(v)?)
        }
        Value::BundledPartialSwap(v) => spec::vendor_marks::VendorLevel::BundledPartialSwap(
            decode_bundled_partial_swap_mark(v)?,
        ),
    })
}

pub fn encode_swap_mark(value: &spec::vendor_marks::SwapMark) -> pb::SwapMark {
    pb::SwapMark {
        package: Some(value.package.clone()),
        version: Some(value.version.clone()),
        subpath: Some(value.subpath.clone()),
        wrapper_shape: value.wrapper_shape.map(|shape| shape as i32),
        default_export_aliases: value.default_export_aliases.clone(),
    }
}

pub fn decode_swap_mark(v: pb::SwapMark) -> Result<spec::vendor_marks::SwapMark> {
    Ok(spec::vendor_marks::SwapMark {
        package: required(v.package)?,
        version: required(v.version)?,
        subpath: required(v.subpath)?,
        wrapper_shape: v
            .wrapper_shape
            .map(pb::WrapperShape::try_from)
            .transpose()?,
        default_export_aliases: v.default_export_aliases,
    })
}

pub fn encode_partial_swap_mark(
    value: &spec::vendor_marks::PartialSwapMark,
) -> pb::PartialSwapMark {
    pb::PartialSwapMark {
        packages: encode_map(&value.packages, encode_partial_swap_package),
        symbols: encode_map(&value.symbols, encode_partial_swap_symbol),
    }
}

pub fn decode_partial_swap_mark(
    v: pb::PartialSwapMark,
) -> Result<spec::vendor_marks::PartialSwapMark> {
    Ok(spec::vendor_marks::PartialSwapMark {
        packages: decode_map(v.packages, decode_partial_swap_package)?,
        symbols: decode_map(v.symbols, decode_partial_swap_symbol)?,
    })
}

pub fn encode_partial_swap_package(
    value: &spec::vendor_marks::PartialSwapPackage,
) -> pb::PartialSwapPackage {
    pb::PartialSwapPackage {
        version: Some(value.version.clone()),
        subpath: Some(value.subpath.clone()),
        namespace: value.namespace.clone(),
    }
}

pub fn decode_partial_swap_package(
    v: pb::PartialSwapPackage,
) -> Result<spec::vendor_marks::PartialSwapPackage> {
    Ok(spec::vendor_marks::PartialSwapPackage {
        version: required(v.version)?,
        subpath: required(v.subpath)?,
        namespace: v.namespace,
    })
}

pub fn encode_partial_swap_symbol(
    value: &spec::vendor_marks::PartialSwapSymbol,
) -> pb::PartialSwapSymbol {
    pb::PartialSwapSymbol {
        package: Some(value.package.clone()),
        kind: Some(value.kind as i32),
        upstream_export: value.upstream_export.clone(),
        local: value.local.clone(),
    }
}

pub fn decode_partial_swap_symbol(
    v: pb::PartialSwapSymbol,
) -> Result<spec::vendor_marks::PartialSwapSymbol> {
    Ok(spec::vendor_marks::PartialSwapSymbol {
        package: required(v.package)?,
        kind: pb::PartialSwapKind::try_from(required(v.kind)?)?,
        upstream_export: v.upstream_export,
        local: v.local,
    })
}

pub fn encode_bundled_partial_swap_mark(
    value: &spec::vendor_marks::BundledPartialSwapMark,
) -> pb::BundledPartialSwapMark {
    pb::BundledPartialSwapMark {
        bundle: Some(encode_bundled_partial_swap_bundle(&value.bundle)),
        packages: encode_map(&value.packages, encode_bundled_partial_swap_package),
        symbols: encode_map(&value.symbols, encode_partial_swap_symbol),
    }
}

pub fn decode_bundled_partial_swap_mark(
    v: pb::BundledPartialSwapMark,
) -> Result<spec::vendor_marks::BundledPartialSwapMark> {
    Ok(spec::vendor_marks::BundledPartialSwapMark {
        bundle: decode_bundled_partial_swap_bundle(required(v.bundle)?)?,
        packages: decode_map(v.packages, decode_bundled_partial_swap_package)?,
        symbols: decode_map(v.symbols, decode_partial_swap_symbol)?,
    })
}

pub fn encode_bundled_partial_swap_bundle(
    value: &spec::vendor_marks::BundledPartialSwapBundle,
) -> pb::BundledPartialSwapBundle {
    pb::BundledPartialSwapBundle {
        path: Some(encode_path(&value.path)),
    }
}

pub fn decode_bundled_partial_swap_bundle(
    v: pb::BundledPartialSwapBundle,
) -> Result<spec::vendor_marks::BundledPartialSwapBundle> {
    Ok(spec::vendor_marks::BundledPartialSwapBundle {
        path: decode_path(required(v.path)?),
    })
}

pub fn encode_bundled_partial_swap_package(
    value: &spec::vendor_marks::BundledPartialSwapPackage,
) -> pb::BundledPartialSwapPackage {
    pb::BundledPartialSwapPackage {
        version: Some(value.version.clone()),
        subpath: Some(value.subpath.clone()),
        bundle_export: Some(value.bundle_export.clone()),
        namespace: value.namespace.clone(),
    }
}

pub fn decode_bundled_partial_swap_package(
    v: pb::BundledPartialSwapPackage,
) -> Result<spec::vendor_marks::BundledPartialSwapPackage> {
    Ok(spec::vendor_marks::BundledPartialSwapPackage {
        version: required(v.version)?,
        subpath: required(v.subpath)?,
        bundle_export: required(v.bundle_export)?,
        namespace: v.namespace,
    })
}
