# Forecasting-gym agent sandbox. The contestant agent runs bash here; python3 is
# on PATH with a data-analysis + modeling toolkit baked in (no network at runtime,
# so everything it needs must be in the image). This file is shown to the agent
# verbatim in its system prompt (loom/gym/inspect_harness.py embeds it), so it is
# the single source of truth for the inventory — there is no separate list to sync.
FROM python:3.13-slim

# bash for the agent's bash tool; curl + ca-certificates for HTTPS fetches; g++ so
# pytensor (pymc's backend) can compile its C ops at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash curl ca-certificates g++ \
    && rm -rf /var/lib/apt/lists/*

# Pinned and resolved together for python 3.13: core data analysis (pandas, numpy,
# scipy, statsmodels), probabilistic + ML modeling (scikit-learn, pymc, arviz,
# arch), and fetch + HTML parsing (requests, beautifulsoup4, lxml).
RUN pip install --no-cache-dir \
    pandas==2.2.3 \
    numpy==2.2.1 \
    scipy==1.15.1 \
    statsmodels==0.14.4 \
    python-dateutil==2.9.0.post0 \
    scikit-learn==1.9.0 \
    pymc==6.0.1 \
    arviz==1.1.0 \
    arch==8.0.0 \
    requests==2.32.3 \
    beautifulsoup4==4.15.0 \
    lxml==6.1.1

CMD ["bash"]
