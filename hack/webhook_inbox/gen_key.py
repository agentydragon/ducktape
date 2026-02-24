#!/usr/bin/env python3
"""Print a fresh 44-char Fernet key to stdout."""

from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(Fernet.generate_key().decode())
