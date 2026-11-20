"""Macro: data_uri_module — wrap binary assets as base64 data-URI TS constants."""

load("//devinfra/js:ts_library.bzl", "ts_library")

def data_uri_module(name, consts, tsconfig):
    """Generate one ts_library exporting each `consts` key as a base64 data: URI of its file.

    Runs `//devinfra/js:data_uri_bin` (base64-encodes each source and guesses its MIME type from the
    file extension) to produce `<name>.ts`, a single file with one `export const` per entry, compiled
    like any other module so importers see its declarations.

    Args:
        name:       Name of the output `ts_library` target.
        consts:     dict of `{exported TypeScript constant name: source asset file label}`
                    (e.g. an `http_file` repo's `//file`, or a checked-in source file).
        tsconfig:   Label of the `ts_config` the generated module compiles against.

    Example:
        data_uri_module(
            name = "brand_icon_data",
            consts = {
                "GMAIL_ICON_DATA_URI": "@gmail_icon_svg//file",
                "GOOGLE_CALENDAR_ICON_DATA_URI": "@google_calendar_icon_svg//file",
            },
            tsconfig = ":tsconfig",
        )
    """
    native.genrule(
        name = "_" + name + "_generate",
        srcs = consts.values(),
        outs = [name + ".ts"],
        cmd = "$(location //devinfra/js:data_uri_bin) -o $@ " +
              " ".join(["{}=$(location {})".format(const_name, src) for const_name, src in consts.items()]),
        tools = ["//devinfra/js:data_uri_bin"],
    )

    ts_library(
        name = name,
        srcs = [":_" + name + "_generate"],
        tsconfig = tsconfig,
        tags = ["no-lint"],
    )
