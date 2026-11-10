"""TypeScript bindings for protos using google.api.http annotations."""

load("@aspect_rules_ts//ts:proto.bzl", "ts_proto_library")

_ANNOTATIONS = Label("//third_party/googleapis:annotations_pb")
_ANNOTATIONS_REWRITE = "./google/api/annotations_pb.js:./" + _ANNOTATIONS.package

def http_annotated_ts_proto_library(name, deps = [], protoc_gen_options = {}, **kwargs):
    """Generate TypeScript bindings for a proto importing google/api/annotations.proto."""
    options = dict(protoc_gen_options)
    existing = options.get("rewrite_imports")
    options["rewrite_imports"] = (existing + ",rewrite_imports=" if existing else "") + _ANNOTATIONS_REWRITE
    ts_proto_library(
        name = name,
        deps = deps + [_ANNOTATIONS],
        protoc_gen_options = options,
        **kwargs
    )
