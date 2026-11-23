import os

from ollama_bench import run

if __name__ == "__main__":
    effort = os.environ.get("REASONING_EFFORT", "off")
    # "off" is native to Ollama; it is not an OpenAI reasoning_effort value.
    fields = {"think": False} if effort == "off" else {"reasoning_effort": effort, "think": effort}
    run(effort, fields, ("reasoning_content", "reasoning", "thinking"))
