# Forecasting-gym agent sandbox. The contestant agent runs bash here; python3
# is on PATH with a data-analysis toolkit baked in (no network at runtime, so
# everything it needs must be in the image). The agent is told this inventory
# in its system prompt — keep loom/gym/inspect_harness.py:AGENT_PROMPT in sync.
FROM python:3.13-slim

RUN pip install --no-cache-dir \
    pandas==2.2.3 \
    numpy==2.2.1 \
    scipy==1.15.1 \
    statsmodels==0.14.4 \
    python-dateutil==2.9.0.post0 \
    requests==2.32.3

# bash for the agent's bash tool; curl/ca-certificates for proxy fetches.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

CMD ["bash"]
