import os

from ollama_bench import run

if __name__ == "__main__":
    effort = os.environ.get("REASONING_EFFORT", "low")
    run(effort, {"reasoning_effort": effort, "think": False if effort == "off" else effort}, ("reasoning_content",))
