"""Defaults for unauthenticated GitHub repository visibility lookups."""

API_BASE_URL = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 10.0
# Visibility is stable in the common case, and the unauthenticated GitHub REST API is rate-limited
# to 60 requests/hour total — a day bounds staleness (a repo flipping public/private takes up to
# this long to reflect) without spending that shared budget on every repeat check.
CACHE_TTL_SECONDS = 86400.0
