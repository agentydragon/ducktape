# LLM Instructions Web Server

FastAPI server that serves LLM instructions with a verification mechanism: checksum pieces are scattered throughout the document, and the LLM must collect all 7 pieces to assemble a verification URL proving complete reading.

Token includes timestamp + document hash for uniqueness per reading. Currently only `index.md` uses the scattered tags.

## Running

```bash
python html_server.py
```

## Environment Variables

- `TOKEN_SECRET`: secret for token generation (default: `hunter2`)
- `PORT`: listen port (default: `9000`)
- `SITE_URL`: base URL for verification links
