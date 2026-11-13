# Action Service API contracts

This directory owns the protobuf definitions for the Action Service, operator Action API, OAuth
linkage callback. The generated Go, Python, and TypeScript targets live
beside their source contracts; service implementations depend on these targets.

The source move changes protobuf descriptor filenames and generated language import paths. The
protobuf package remains `agentplane.action_service`, so message names, RPC names, HTTP annotations,
authorization behavior, and wire fields stay unchanged.
