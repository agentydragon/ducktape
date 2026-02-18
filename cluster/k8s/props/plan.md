# Props cluster deployment — TODOs

- [ ] **Seal the OpenAI API key**: The SealedSecret at
      `props-secrets/openai-api-key-sealed.yaml` has a placeholder `REPLACE_ME`
      value. Seal with:
      `scripts/seal-secret.sh props props-openai-api-key api-key <your-openai-key>`
- [x] Ensure Ollama has `gpt-oss-20b` model pulled — automated via `cluster/k8s/ollama/job-pull-gpt-oss-20b-v1.yaml`
