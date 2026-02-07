# common-lib

Library chart that centralizes shared Helm helpers (labels, naming, Postgres templates, sealed-secret helpers) for services in this repo. Import it as a dependency and reference the helpers via `{{- include "common.labels" . }}` style calls.
