@README.md

# Agent Guide for `tana`

## Gotchas

- Navigation methods (`child_nodes`, `get_supertags`, `get_language`) live on `TanaGraph`, not on node models. Domain nodes are pure data.
- Prefer absolute imports (`tana.query.search_parser`, etc.) to keep layering clear.
- CLI scripts expect JSON dumps that mirror Tana’s export structure (`docs` array with node dicts).
