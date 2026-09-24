# Build of upstream iron-proxy pinned to the v0.50.0 release commit, the first
# stable release with HTTP/2/gRPC support in MITM mode. Nothing here is specific to
# any one consumer; two egress fences share this image (public-coder-agent,
# haku-openclaw-spike-proxy).
#
# The workflow supplies the exact checked-out source tree as this build context;
# do not fetch a branch or mutable tag here.
#
# v0.50.0 is now a stable release, so consumers could return to the official
# upstream image; whether to is the supply-chain decision in
# plans/personal_agents/TODO.md ("Decide deliberately how the iron-proxy image is
# pinned"), which moved production off Docker Hub on purpose.
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
