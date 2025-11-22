{
  title: 'generate_frontend_code.py should inline helper function and use list literals',
  severity: 'minor',
  category: 'code-style',
  locations: [
    {
      path: 'adgn/scripts/generate_frontend_code.py',
      lines: [183, 186],
      context: 'get_json_schema function is used only once',
    },
    {
      path: 'adgn/scripts/generate_frontend_code.py',
      lines: [255],
      context: 'main_schema variable assigned and used once',
    },
    {
      path: 'adgn/scripts/generate_frontend_code.py',
      lines: [268, 274],
      context: 'ts_output built via imperative appends',
    },
  ],
  description: |||
    The generate_frontend_code.py script has several minor style issues:

    **1. get_json_schema should be inlined (lines 183-186)**
    ```python
    def get_json_schema(model: type) -> dict[str, Any]:
        """Get JSON Schema for a Pydantic model."""
        adapter = TypeAdapter(model)
        return adapter.json_schema(mode="serialization")
    ```
    This 3-line helper is used exactly once at line 250. It should be inlined.

    **2. main_schema should be inlined (line 255)**
    ```python
    main_schema = {k: v for k, v in schema.items() if k != "$defs"}
    all_defs[type_name] = main_schema
    ```
    The `main_schema` variable is assigned and immediately used once. Should inline:
    ```python
    all_defs[type_name] = {k: v for k, v in schema.items() if k != "$defs"}
    ```

    **3. ts_output should use list literal (lines 268-274)**
    ```python
    ts_output = []
    ts_output.append("// Auto-generated TypeScript types from Pydantic models")
    ts_output.append("// Do not edit manually - regenerate with: npm run codegen")
    ts_output.append("")
    ts_output.append(ts_code.strip())
    ```
    This should be a list literal:
    ```python
    ts_output = [
        "// Auto-generated TypeScript types from Pydantic models",
        "// Do not edit manually - regenerate with: npm run codegen",
        "",
        ts_code.strip(),
    ]
    ```
  |||,
  recommendation: |||
    Apply these inline simplifications:

    1. Remove get_json_schema helper, inline at line 250:
    ```python
    schema = TypeAdapter(model_class).json_schema(mode="serialization")
    ```

    2. Inline main_schema at line 256:
    ```python
    all_defs[type_name] = {k: v for k, v in schema.items() if k != "$defs"}
    ```

    3. Replace ts_output construction with list literal:
    ```python
    ts_output = [
        "// Auto-generated TypeScript types from Pydantic models",
        "// Do not edit manually - regenerate with: npm run codegen",
        "",
        ts_code.strip(),
    ]
    output_file.write_text("\n".join(ts_output))
    ```

    These changes reduce indirection and make the code more direct.
  |||,
}
