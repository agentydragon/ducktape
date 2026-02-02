#!/bin/sh
# Wrapper around dockerd for Firecracker VMs lacking CONFIG_IP_NF_RAW.
# Docker 28+ creates iptables raw table rules for published ports (-p),
# which fails without that kernel module. This env var (Docker 28.0.2+,
# moby #49621) skips all raw table operations.
# See: tools/rbe_image/docs/firecracker_docker_init_timeout.md

export DOCKER_INSECURE_NO_IPTABLES_RAW=1
exec /usr/bin/dockerd.real "$@"
