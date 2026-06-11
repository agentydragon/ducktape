{ pkgs, ... }:
{
  packages = [
    pkgs.curl
    pkgs.jq
    pkgs.uv # For vLLM installation
  ];

  env.OLLAMA_MODELS = "/wyrmhdd/ollama-models";
  env.OLLAMA_HOST = "127.0.0.1:11434";

  # vLLM model cache (HuggingFace models)
  env.HF_HOME = "/wyrmhdd/huggingface";

  enterShell = ''
    echo "Local LLM environment (Ollama + vLLM)"
    echo ""
    echo "=== Ollama (single-GPU, GGUF) ==="
    echo "  Model storage: $OLLAMA_MODELS"
    echo "  ollama serve        - Start server (or use systemd: systemctl status ollama)"
    echo "  ollama pull <model> - Download model"
    echo "  ollama run <model>  - Interactive chat"
    echo "  ollama list         - List models"
    echo ""
    echo "=== vLLM (tensor parallel, HuggingFace) ==="
    echo "  Model cache: $HF_HOME"
    echo "  ./start-vllm.sh     - Start vLLM server on port 8000"
    echo ""
    echo "To install vLLM (first time only):"
    echo "  uv pip install vllm --system"
    echo ""
  '';
}
