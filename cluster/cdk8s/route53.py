"""The Route 53 hosted zones the cluster manages records and ACME challenges in."""

from __future__ import annotations

from types import MappingProxyType

# Domain -> hosted zone ID. Every zone here must also be in the cluster-external-dns and
# cert-manager IAM policies (cluster/docs/iam-policy-*.json), which live in AWS, not here.
HOSTED_ZONES = MappingProxyType(
    {
        "allegedly.works": "Z02901943N8ZFQFOD9P5I",
        "agentydragon.com": "ZPL94Z1ZHF81X",
        "adgn.link": "Z084833315EMAW4JJW9NM",
    }
)
