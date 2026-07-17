"""Macro: data_uri_module — wrap binary assets as base64 data-URI TS constants."""

load("@aspect_rules_js//js:defs.bzl", "js_library")

def data_uri_module(name, consts, out = None, visibility = None):
    """Generate one js_library exporting each `consts` key as a base64 data: URI of its file.

    Runs `//util:data_uri_bin` (base64-encodes each source and guesses its MIME type from the
    file extension) to produce a single `.ts` file with one `export const` per entry, wrapped in
    a `js_library`.

    Args:
        name:       Name of the output `js_library` target.
        consts:     dict of `{exported TypeScript constant name: source asset file label}`
                    (e.g. an `http_file` repo's `//file`, or a checked-in source file).
        out:        Package-relative path for the generated `.ts` file. Defaults to `<name>.ts`.
        visibility: Visibility of the output `js_library` target.

    Example:
        data_uri_module(
            name = "brand_icon_data",
            consts = {
                "GMAIL_ICON_DATA_URI": "@gmail_icon_svg//file",
                "GOOGLE_CALENDAR_ICON_DATA_URI": "@google_calendar_icon_svg//file",
            },
        )
    """
    out = out or (name + ".ts")

    native.genrule(
        name = "_" + name + "_generate",
        srcs = consts.values(),
        outs = [out],
        cmd = "$(location //util:data_uri_bin) -o $@ " +
              " ".join(["{}=$(location {})".format(const_name, src) for const_name, src in consts.items()]),
        tools = ["//util:data_uri_bin"],
    )

    js_library(
        name = name,
        srcs = [":_" + name + "_generate"],
        tags = ["no-lint"],
        visibility = visibility,
    )
