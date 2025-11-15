#!/usr/bin/env python3
"""Create versions of files WITHOUT type annotation workarounds."""
import re
from pathlib import Path

def remove_uri_annotations(content: str) -> str:
    """Remove the 'uri: str = X; return uri' pattern and replace with 'return X'."""
    # Pattern: spaces + uri: str = SOMETHING + newline + spaces + return uri
    pattern = r'(\s+)uri: str = (.+)\n\s+return uri'
    replacement = r'\1return \2'
    return re.sub(pattern, replacement, content)

def remove_name_annotations(content: str) -> str:
    """Remove the 'name: str = X; return name' pattern and replace with 'return X'."""
    pattern = r'(\s+)name: str = (.+)\n\s+return name'
    replacement = r'\1return \2'
    return re.sub(pattern, replacement, content)

def remove_content_annotations(content: str) -> str:
    """Remove 'content: str; content, x = ...; return content' patterns."""
    # Pattern for multiline: content: str\n    content, _version = ...
    pattern1 = r'(\s+)content: str\n\s+content, (_\w+) = (.+)\n\s+return content'
    replacement1 = r'\1content, \2 = \3\n\1return content'

    # Pattern for single line: content: str = x.content; return content
    pattern2 = r'(\s+)content: str = (.+)\n\s+return content'
    replacement2 = r'\1return \2'

    result = re.sub(pattern1, replacement1, content)
    result = re.sub(pattern2, replacement2, result)
    return result

def main():
    base_dir = Path(__file__).parent / "src/adgn"

    # Process uris.py
    uris_file = base_dir / "mcp/_shared/uris.py"
    content = uris_file.read_text()
    clean_content = remove_uri_annotations(content)
    (uris_file.parent / "uris_clean.py").write_text(clean_content)
    print(f"Created {uris_file.parent / 'uris_clean.py'}")

    # Process docker_env.py
    docker_file = base_dir / "props/docker_env.py"
    content = docker_file.read_text()
    clean_content = remove_name_annotations(content)
    (docker_file.parent / "docker_env_clean.py").write_text(clean_content)
    print(f"Created {docker_file.parent / 'docker_env_clean.py'}")

    print("\nNow you can:")
    print("1. Test WITH annotations: mypy --config-file=pyproject.toml src/adgn/")
    print("2. Swap to clean versions:")
    print("   mv src/adgn/mcp/_shared/uris.py src/adgn/mcp/_shared/uris_annotated.py")
    print("   mv src/adgn/mcp/_shared/uris_clean.py src/adgn/mcp/_shared/uris.py")
    print("   mv src/adgn/props/docker_env.py src/adgn/props/docker_env_annotated.py")
    print("   mv src/adgn/props/docker_env_clean.py src/adgn/props/docker_env.py")
    print("3. Test WITHOUT annotations: mypy --config-file=pyproject.toml src/adgn/")

if __name__ == "__main__":
    main()
