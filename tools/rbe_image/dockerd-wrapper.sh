#!/bin/sh
# Wrapper around dockerd for Firecracker VMs lacking CONFIG_IP_NF_RAW.
#
# Docker 28+ adds iptables "raw" table rules for direct access filtering
# when publishing ports. Firecracker's guest kernel lacks CONFIG_IP_NF_RAW,
# so any container with published ports (-p) fails.
#
# Fix: DOCKER_INSECURE_NO_IPTABLES_RAW=1 (env var, Docker 28.0.2+, moby #49621)
# Skips ALL raw table operations (create and delete).
#
# Note: --allow-direct-routing is a Docker 28.2.0+ feature (moby #49832).
# The base image has Docker 28.1.0, so that flag is NOT available — passing
# it causes "unknown flag" and immediate exit (invisible because goinit
# swallows stderr).
#
# BuildBuddy's goinit has no exec property to pass custom dockerd env vars.
# This wrapper intercepts the dockerd call to set the env var.
#
# Shell (not Python) because goinit's 30s Docker init timeout is too tight
# for Python interpreter startup inside Firecracker VMs.

export DOCKER_INSECURE_NO_IPTABLES_RAW=1
exec /usr/bin/dockerd.real "$@"
