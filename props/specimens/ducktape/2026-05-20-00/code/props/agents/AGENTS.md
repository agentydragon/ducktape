@README.md

# Agent Guide

This directory contains code that runs inside agent containers. Do not add code
here that is consumed by orchestration, backend, or CLI — those belong in
`props/core/` or `props/orchestration/`.

If you need to share types between agents and host-side code, define them in
`props/core/` and import into both places.
