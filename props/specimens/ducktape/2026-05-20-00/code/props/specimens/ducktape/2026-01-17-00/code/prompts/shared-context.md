# Shared Context for Code Quality Scans

## Philosophy

- **Type safety without ugliness**: Python's type system should enhance code clarity, not obscure it
- **Trust library types**: Well-typed libraries (OpenAI SDK, Pydantic, etc.) provide excellent types - use them as intended
- **Read the source**: When uncertain about types, read the actual library source code to understand intended usage patterns
- **Fail fast**: Prefer methods that raise exceptions over getters that hide errors
- **No redundancy**: Every line of code should add value; remove trivial wrappers and obvious documentation

## Tools Available

- **mypy**: Type checker - use to verify type safety
- **ruff**: Fast linter - catches many code quality issues
- **vulture**: Dead code detector
- **AST analysis**: Python's `ast` module for structural analysis
- **grep/ripgrep**: Pattern matching in codebases

## Analysis Workflow

1. **Identify**: Use tools and patterns to find antipatterns
2. **Verify**: Check if the pattern is actually problematic (read library source if needed)
3. **Propose fix**: Suggest proper, type-safe alternative
4. **Validate**: Ensure fix passes mypy and maintains correctness

## Common Library Type Patterns

### OpenAI SDK

- Generally very well-typed
- Use `Response`, `ResponseOutputMessage`, etc. directly
- TypeAdapter for validation, not casts
- Read `openai/types/` for actual type definitions

### Pydantic

- `model_dump(mode="json")` for serialization
- `model_validate()` for parsing
- TypeAdapter for non-model types
- Avoid manual field-by-field serialization

### SQLAlchemy

- Use proper relationship typing
- Consider better-stubs if ORM types are problematic
- Avoid runtime hasattr/getattr when types are known
