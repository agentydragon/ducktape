# Repository Template

**⚠️ This is a template! When initializing your project, keep only the parts relevant to your language/stack and delete the rest.**

For example:

- Starting a Python project? Keep Python sections, delete C++/Rust/JavaScript sections
- Building a Rust CLI? Keep Rust sections, delete Python/web sections
- This README itself should be edited to reflect your actual project

## Structure

```
.
├── docs/               # Documentation
├── references/         # Reference materials (gitignored except fetch.sh)
│   └── fetch.sh       # Script to fetch/update reference materials
├── scratch/           # Temporary agent workspace (gitignored)
├── .gitignore         # Git ignore rules
└── .pre-commit-config.yaml  # Pre-commit hooks configuration
```

## Getting Started

1. Install pre-commit hooks (already done):

   ```bash
   pre-commit install
   ```

2. Update reference materials:

   ```bash
   cd references && ./fetch.sh
   ```

## Development

### Pre-commit Hooks

This repository uses pre-commit hooks to maintain code quality:

- Trailing whitespace removal
- End of file fixing
- YAML/JSON/TOML validation
- Python formatting with Black
- Python linting with Ruff
- Type checking with mypy

Run manually: `pre-commit run --all-files`

### Reference Materials

The `references/` directory contains fetched documentation and example code.
Update with: `cd references && ./fetch.sh`

### Scratch Directory

The `scratch/` directory is for temporary work and experiments. It's gitignored.

---

## 🐍 Python Project Setup [TEAR-OFF IF NOT PYTHON]

### Directory Structure

```
.
├── pyproject.toml          # Project configuration
├── venv/                   # Virtual environment (gitignored)
├── src/                    # Source code
│   └── mypackage/
│       ├── __init__.py
│       ├── module.py       # Implementation
│       └── test_module.py  # Tests go NEXT TO code
├── scripts/                # Standalone scripts
└── requirements.txt        # Direct dependencies (optional with pyproject.toml)
```

### Initial Setup

**⚠️ CRITICAL: ALWAYS use a virtual environment. NEVER install packages globally!**

```bash
# Create virtual environment (REQUIRED - DO THIS FIRST!)
python -m venv venv

# Activate it (YOU MUST DO THIS IN EVERY TERMINAL SESSION)
source venv/bin/activate

# Verify you're in venv (should show .../venv/bin/python)
which python

# Create pyproject.toml
cat > pyproject.toml << 'EOF'
[project]
name = "myproject"
version = "0.1.0"
description = "Project description"
dependencies = []

[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "pytest-asyncio",
    "pytest-cov",
    "black",
    "ruff",
    "mypy",
]

[tool.pytest.ini_options]
testpaths = ["."]
python_files = "test_*.py"

[tool.black]
line-length = 100

[tool.ruff]
line-length = 100
select = ["E", "F", "I", "N", "W", "UP"]

[tool.mypy]
python_version = "3.11"
strict = true
EOF

# Install dev dependencies
pip install -e ".[dev]"
```

### Testing Convention

**CRITICAL**: Tests live NEXT TO the code they test:

- `src/mypackage/foo.py` → `src/mypackage/test_foo.py`
- `src/mypackage/bar/baz.py` → `src/mypackage/bar/test_baz.py`
- NOT in separate `/tests/` directory
- NOT as `/src/test_mypackage_foo.py`
- ONLY as `test_*.py` in the same directory

### Additional Pre-commit Hooks for Python

Add these to `.pre-commit-config.yaml`:

```yaml
- repo: https://github.com/PyCQA/docformatter
  rev: v1.7.5
  hooks:
    - id: docformatter
      args: [--in-place]

- repo: https://github.com/pycqa/isort
  rev: 5.13.2
  hooks:
    - id: isort
      args: ["--profile", "black"]

- repo: https://github.com/asottile/pyupgrade
  rev: v3.15.0
  hooks:
    - id: pyupgrade
      args: [--py311-plus]
```

[END PYTHON SECTION - DELETE ABOVE IF NOT USING PYTHON]

---

## 🦀 Rust Project Setup [TEAR-OFF IF NOT RUST]

### Directory Structure

```
.
├── Cargo.toml             # Project manifest
├── src/
│   ├── main.rs           # Binary entry point
│   └── lib.rs            # Library code
├── tests/                # Integration tests
│   └── integration.rs
└── benches/              # Benchmarks
    └── benchmark.rs
```

### Initial Setup

```bash
# Initialize Rust project
cargo init --name myproject

# Basic Cargo.toml
cat > Cargo.toml << 'EOF'
[package]
name = "myproject"
version = "0.1.0"
edition = "2021"

[dependencies]

[dev-dependencies]
criterion = "0.5"

[[bench]]
name = "benchmark"
harness = false
EOF

# Add to .pre-commit-config.yaml
cat >> .pre-commit-config.yaml << 'EOF'
  - repo: local
    hooks:
      - id: cargo-fmt
        name: cargo fmt
        entry: cargo fmt --
        language: system
        types: [rust]
      - id: cargo-clippy
        name: cargo clippy
        entry: cargo clippy -- -D warnings
        language: system
        types: [rust]
        pass_filenames: false
EOF
```

[END RUST SECTION - DELETE ABOVE IF NOT USING RUST]

---

## 📦 JavaScript/TypeScript Setup [TEAR-OFF IF NOT JS/TS]

### Directory Structure

```
.
├── package.json           # Project manifest
├── tsconfig.json         # TypeScript config (if using TS)
├── src/
│   ├── index.ts
│   └── index.test.ts     # Tests next to code
├── dist/                 # Build output (gitignored)
└── node_modules/         # Dependencies (gitignored)
```

### Initial Setup

```bash
# Initialize package
npm init -y

# TypeScript setup
npm install --save-dev typescript @types/node jest @types/jest ts-jest

# Create tsconfig.json
cat > tsconfig.json << 'EOF'
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "commonjs",
    "lib": ["ES2022"],
    "outDir": "./dist",
    "rootDir": "./src",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true
  }
}
EOF

# Add to .pre-commit-config.yaml
cat >> .pre-commit-config.yaml << 'EOF'
  - repo: https://github.com/pre-commit/mirrors-eslint
    rev: v8.56.0
    hooks:
      - id: eslint
        files: \.[jt]sx?$
        types: [file]
        additional_dependencies:
          - eslint@8.56.0
          - eslint-config-standard
EOF
```

[END JAVASCRIPT SECTION - DELETE ABOVE IF NOT USING JS/TS]

---

## 🌐 Generic Language Setup [CUSTOMIZE OR DELETE]

For other languages, ensure you:

1. Set up the standard project structure for that language
2. Configure the build system (Maven, Gradle, CMake, etc.)
3. Add language-specific linters to `.pre-commit-config.yaml`
4. Follow the testing convention of that language's ecosystem
5. Update this README to reflect actual project structure

[END GENERIC SECTION]
