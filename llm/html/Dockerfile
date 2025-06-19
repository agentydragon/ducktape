FROM python:3.11-slim AS runtime

# Avoid buffering Python stdout/stderr
ENV PYTHONUNBUFFERED=1

# Install curl for health-check ping
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Only copy dependency manifests first to leverage Docker layer cache
COPY llm/html/pyproject.toml llm/html/requirements.txt ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the application code from the build context
# Check if we're building from repo root (build_and_run.sh) or from llm/html (deploy_dev.sh)
# First, try the repo root context
COPY llm/html/llm_html/ ./llm_html/
COPY llm/html/index.md llm/html/style.css llm/html/base.html llm/html/verify.html llm/html/stats.html llm/html/tana.md ./
COPY dotfiles/codex/instructions.md ./coding.md

EXPOSE 9000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl --fail http://127.0.0.1:9000/ || exit 1

CMD ["python", "-m", "llm_html.server"]
