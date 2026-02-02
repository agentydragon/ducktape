#!/bin/sh
# Wrapper around dockerd for Firecracker VMs lacking CONFIG_IP_NF_RAW.
#
# Docker 28+ adds iptables "raw" table rules for direct access filtering
# when publishing ports. Firecracker's guest kernel lacks CONFIG_IP_NF_RAW,
# so any container with published ports (-p) fails.
#
# Two complementary workarounds:
#   DOCKER_INSECURE_NO_IPTABLES_RAW=1  (env var, Docker 28.0.2+)
#     Skips ALL raw table operations (create and delete). This is the more
#     complete fix — --allow-direct-routing alone doesn't prevent raw table
#     deletion attempts which also fail without the kernel module.
#   --allow-direct-routing  (CLI flag)
#     Tells dockerd to skip direct access filtering DROP rules. Belt and
#     suspenders with the env var.
#
# BuildBuddy's goinit has no exec property to pass custom dockerd flags or
# env vars. This wrapper intercepts the dockerd call to add them.
#
# Shell (not Python) because goinit's 30s Docker init timeout is too tight
# for Python interpreter startup inside Firecracker VMs.

export DOCKER_INSECURE_NO_IPTABLES_RAW=1
exec /usr/bin/dockerd.real --allow-direct-routing "$@"
