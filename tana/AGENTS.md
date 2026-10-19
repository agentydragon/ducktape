@README.md

- Navigation (`child_nodes`, `get_supertags`, `get_language`) lives on `TanaGraph`; domain node
  models are pure data.
- CLI scripts expect JSON mirroring Tana's export structure (`docs` array of node dicts).
