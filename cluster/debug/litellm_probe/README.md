# LiteLLM model probe

[The probe](probe_models.py) sends small text and tool requests to selected configured models to compare supported API shapes. The Bazel target uses the committed proxy configuration by default. An authorized operator can provide an existing key through `LITELLM_API_KEY` and use the model, shape, and scenario flags to bound a run.

```bash
bb run //cluster/debug/litellm_probe:probe_models_bin -- --help
bbr test //cluster/debug/litellm_probe:test_probe_models
```

The probe writes request and response artifacts under `--response-dir`. [The report template](report.html.j2) renders a summary. Use `--report-only` with `--aggregate-dir` to summarize existing artifacts without sending requests.
