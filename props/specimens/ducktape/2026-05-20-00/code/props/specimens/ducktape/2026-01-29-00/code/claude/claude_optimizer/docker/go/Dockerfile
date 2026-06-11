FROM claude-base:latest

# Switch to root to install Go
USER root

# Install Go (architecture-aware)
ARG GO_VERSION=1.21.5
ARG TARGETARCH
RUN curl -L "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" -o \
        "go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" \
    && tar -C /usr/local -xzf "go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" \
    && rm "go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" \
    && chmod -R 755 /usr/local/go

# Switch back to node user
USER node

ENV PATH="/usr/local/go/bin:${PATH}"
ENV GOPATH="/home/node/go"
ENV GOBIN="${GOPATH}/bin"
ENV PATH="${GOBIN}:${PATH}"

# Install common Go tools
RUN go install golang.org/x/tools/cmd/goimports@latest \
    && go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest

LABEL dev.environment="go"
LABEL dev.description="Go development environment with common tools"