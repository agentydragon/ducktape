## Lower-level

- Prefer equivalent formulations with fewer lines and less state when readability is equal or better. Patterns here
  include:
  - Comprehensions over imperative accumulation
  - Inlining throwaway temporaries
  - Deriving output from a single source of truth (constants)
  - Condensing trivial branches

- If running on Python ≥ 3.12, use `Path.walk`:

  ```python
  # Python 3.12+
  for dirpath, dirnames, filenames in Path(root).walk():
      ...
  ```

  For earlier Python versions, `os.walk` remains the portable choice.
