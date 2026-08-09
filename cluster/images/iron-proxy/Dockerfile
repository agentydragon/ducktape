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
FROM golang:1.26.1-alpine3.23@sha256:2389ebfa5b7f43eeafbd6be0c3700cc46690ef842ad962f6c5bd6be49ed82039 AS build

ARG IRON_PROXY_REVISION
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 go build -trimpath \
      -ldflags="-s -w -X github.com/ironsh/iron-proxy/internal/version.Version=${IRON_PROXY_REVISION}" \
      -o /out/iron-proxy ./cmd/iron-proxy

FROM alpine:3.23@sha256:fd791d74b68913cbb027c6546007b3f0d3bc45125f797758156952bc2d6daf40
ARG IRON_PROXY_REVISION
RUN apk add --no-cache ca-certificates openssl
COPY --from=build /out/iron-proxy /usr/local/bin/iron-proxy
LABEL org.opencontainers.image.source="https://github.com/ironsh/iron-proxy" \
      org.opencontainers.image.revision="${IRON_PROXY_REVISION}"
ENTRYPOINT ["iron-proxy"]
