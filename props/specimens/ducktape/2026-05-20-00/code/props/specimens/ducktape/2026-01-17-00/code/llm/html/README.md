# LLM Instructions Web Server

A FastAPI server that serves LLM instructions with a checksum mechanism to verify LLMs actually read the complete document.

## Purpose

This server provides a simple way to serve instructions to an LLM in a way that lets you check the LLM has read your entire document rather than just skimming the beginning or using a cached version.
It works by scattering checksum pieces throughout the document that the LLM must collect and present back as proof.

## How It Works

1. The server dynamically generates a checksum token on each page load
2. This token is split into 7 two-character pieces scattered throughout the document
3. The LLM must find all pieces and assemble them into a verification URL
4. You click the URL to verify the LLM actually read everything

The token includes a timestamp and document hash, making each reading unique.

Note: Currently only `index.md` includes the scattered tags. Other pages could easily use the same mechanism but don't currently.

## Development

See `@AGENTS.md` in the repository root for Bazel build, test, and lint workflows.

## Running

```bash
python html_server.py
```

## Environment Variables

- `TOKEN_SECRET`: Secret for token generation (default: 'hunter2')
- `PORT`: Port to listen on (default: 9000)
- `SITE_URL`: Base URL for verification links
