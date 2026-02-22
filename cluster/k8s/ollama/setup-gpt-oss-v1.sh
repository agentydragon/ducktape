#!/bin/sh
set -e
ollama pull gpt-oss:20b
ollama pull gpt-oss:120b
ollama create gpt-oss-256k -f /modelfiles/Modelfile-256k
ollama create gpt-oss-512k -f /modelfiles/Modelfile-512k
