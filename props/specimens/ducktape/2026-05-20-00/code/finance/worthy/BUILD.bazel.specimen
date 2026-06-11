load(
    "@rules_rust//rust:defs.bzl",
    "rust_binary",
    #"rust_doc",
    "rust_library",
    "rust_test",
)

rust_library(
    name = "source",
    srcs = ["source.rs"],
    edition = "2024",
    proc_macro_deps = [
        "@crates//:async-trait",
    ],
    visibility = ["//visibility:public"],
    deps = [
        ":asset",
    ],
)

rust_library(
    name = "converter",
    srcs = ["converter.rs"],
    edition = "2024",
    proc_macro_deps = [
        "@crates//:async-trait",
    ],
    visibility = ["//visibility:public"],
    deps = [
        ":denomination",
        ":exchange_rate",
    ],
)

rust_library(
    name = "exchange_rate",
    srcs = ["exchange_rate.rs"],
    edition = "2024",
    visibility = ["//visibility:public"],
    deps = [
        ":denomination",
        "@crates//:chrono",
        "@crates//:chrono-tz",
        "@crates//:rust_decimal",
    ],
)

rust_binary(
    name = "rust_main",
    srcs = ["main.rs"],
    edition = "2024",
    proc_macro_deps = [
        "@crates//:rust_decimal_macros",
    ],
    deps = [
        ":asset",
        ":common_currency",
        ":config",
        ":converter",
        ":denomination",
        ":exchange_rate",
        ":flags",
        ":json_output",
        ":source",
        "//finance/worthy/converter:alphavantage_converter",
        "//finance/worthy/converter:currencylayer_converter",
        "//finance/worthy/converter:fixer_converter",
        "//finance/worthy/model:model_rs",
        "//finance/worthy/source:ibflex_source",
        "@crates//:chrono",
        "@crates//:csv",
        "@crates//:env_logger",
        "@crates//:futures",
        "@crates//:glob",
        "@crates//:log",
        "@crates//:reqwest",
        "@crates//:rust_decimal",
        "@crates//:rusty-money",
        "@crates//:scraper",
        "@crates//:serde_json",
        "@crates//:serde_yaml",
        "@crates//:shellexpand",
        "@crates//:structopt",
        "@crates//:term-table",
        "@crates//:tokio",
        "@crates//:xdg",
    ],
)

rust_library(
    name = "common_currency",
    srcs = ["common_currency.rs"],
    edition = "2024",
    proc_macro_deps = [
        "@crates//:rust_decimal_macros",
    ],
    visibility = ["//visibility:public"],
    deps = [
        ":denomination",
        ":exchange_rate",
        "@crates//:log",
        "@crates//:petgraph",
        "@crates//:rust_decimal",
    ],
)

rust_test(
    name = "common_currency_test",
    srcs = ["common_currency_test.rs"],
    edition = "2024",
    proc_macro_deps = [
        "@crates//:rust_decimal_macros",
    ],
    deps = [
        ":common_currency",
        ":denomination",
        ":exchange_rate",
        "@crates//:env_logger",
        "@crates//:log",
        "@crates//:rust_decimal",
    ],
)

rust_library(
    name = "ibflex",
    srcs = ["ibflex.rs"],
    edition = "2024",
    visibility = ["//visibility:public"],
    deps = [
        "@crates//:log",
        "@crates//:reqwest",
        "@crates//:rust_decimal",
        "@crates//:serde",
        "@crates//:serde-xml-rs",
        "@crates//:tokio",
        "@crates//:url",
    ],
)

rust_test(
    name = "ibflex_test",
    srcs = ["ibflex_test.rs"],
    edition = "2024",
    deps = [
        ":ibflex",
        "@crates//:rust_decimal",
        "@crates//:url",
    ],
)

#rust_doc(
#    name = "rust_doc",
#    dep = ":rust_main",
#)

rust_test(
    name = "rust_main_test",
    crate = ":rust_main",
)

rust_library(
    name = "denomination",
    srcs = ["denomination.rs"],
    edition = "2024",
    visibility = ["//visibility:public"],
    deps = [
        "@crates//:serde",
    ],
)

rust_library(
    name = "asset",
    srcs = ["asset.rs"],
    edition = "2024",
    visibility = ["//visibility:public"],
    deps = [
        ":denomination",
        "@crates//:rust_decimal",
        "@crates//:serde",
    ],
)

rust_library(
    name = "flags",
    srcs = ["flags.rs"],
    edition = "2024",
    deps = [
        "@crates//:serde",
        "@crates//:structopt",
    ],
)

rust_library(
    name = "config",
    srcs = ["config.rs"],
    edition = "2024",
    deps = [
        ":asset",
        "//finance/worthy/converter:alphavantage_converter",
        "//finance/worthy/converter:currencylayer_converter",
        "//finance/worthy/converter:fixer_converter",
        "//finance/worthy/source:ibflex_source",
        "@crates//:rust_decimal",
        "@crates//:serde",
        "@crates//:serde_yaml",
    ],
)

rust_test(
    name = "flags_test",
    srcs = ["flags_test.rs"],
    edition = "2024",
    deps = [
        ":config",
        ":flags",
        "@crates//:structopt",
    ],
)

rust_library(
    name = "json_output",
    srcs = ["json_output.rs"],
    edition = "2024",
    proc_macro_deps = [
        "@crates//:rust_decimal_macros",
    ],
    deps = [
        "@crates//:chrono",
        "@crates//:rust_decimal",
        "@crates//:serde",
        "@crates//:serde_json",
    ],
)

rust_test(
    name = "json_output_test",
    srcs = ["json_output_test.rs"],
    edition = "2024",
    proc_macro_deps = [
        "@crates//:rust_decimal_macros",
    ],
    deps = [
        ":json_output",
        "@crates//:chrono",
        "@crates//:rust_decimal",
        "@crates//:serde",
        "@crates//:serde_json",
    ],
)
