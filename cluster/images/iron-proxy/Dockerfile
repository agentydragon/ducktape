# Build of upstream iron-proxy pinned to a specific unreleased commit — the
# first one with HTTP/2/gRPC support in MITM mode. Nothing here is specific to
# any one consumer; three egress fences share this image (public-coder-agent,
# haku-claude-oauth-proxy, haku-openclaw-spike-proxy).
#
# The workflow supplies the exact checked-out source tree as this build context;
# do not fetch a branch or mutable tag here.
#
# CLEANUP(added 2026-08-09): delete this build path and return every consumer to
#   the official upstream image once iron-proxy v0.50.0 ships stable. c90f4fe is
#   v0.49.0 (latest stable) + 1 commit and is already in v0.50.0-rc.1/rc.2, so
#   the only thing keeping this here is the lack of a non-RC tag. Note the
#   official image also picks up the two rc commits.
FROM golang:1.27.1-alpine3.24@sha256:cf6fca6641884b8433441b2b0652976f975e1d0fdd26d177eaaf8596087f3125 AS build

ARG IRON_PROXY_REVISION
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 go build -trimpath \
      -ldflags="-s -w -X github.com/ironsh/iron-proxy/internal/version.Version=${IRON_PROXY_REVISION}" \
      -o /out/iron-proxy ./cmd/iron-proxy

FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
ARG IRON_PROXY_REVISION
RUN apk add --no-cache ca-certificates openssl
COPY --from=build /out/iron-proxy /usr/local/bin/iron-proxy
LABEL org.opencontainers.image.source="https://github.com/ironsh/iron-proxy" \
      org.opencontainers.image.revision="${IRON_PROXY_REVISION}"
ENTRYPOINT ["iron-proxy"]
