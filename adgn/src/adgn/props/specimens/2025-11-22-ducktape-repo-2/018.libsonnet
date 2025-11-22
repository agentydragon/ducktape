local I = import '../../specimens/lib.libsonnet';

I.issueOneOccurrence(
  rationale= |||
    The generate_frontend_code.py script has three instances of unnecessary one-off variables that should be inlined:

    **1. get_json_schema helper function (lines 183-186):**
    This 3-line helper wraps TypeAdapter().json_schema() and is used exactly once at line 250. It adds no value beyond the direct call and should be inlined.

    **2. main_schema variable (line 255):**
    The variable is assigned a dict comprehension and immediately used once to update all_defs. This intermediate variable adds no clarity and should be inlined directly into the dict assignment.

    **3. ts_output list construction (lines 268-274):**
    The code creates an empty list and then imperatively appends four items. This should be replaced with a list literal containing all four items, which is more direct and idiomatic Python.

    **Fix:**
    - Remove get_json_schema function and inline the TypeAdapter call at line 250
    - Inline the dict comprehension directly: `all_defs[type_name] = {k: v for k, v in schema.items() if k != "$defs"}`
    - Replace ts_output construction with a list literal containing all four string elements

    These changes eliminate unnecessary indirection and make the code more direct and readable.
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'adgn/scripts/generate_frontend_code.py': [
      [183, 186],  // get_json_schema function
      255,         // main_schema variable
      [268, 274],  // ts_output imperative construction
    ],
  },
)
