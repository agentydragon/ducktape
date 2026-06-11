# FreeCAD Skill Evaluation

Evaluation tasks for testing whether an LLM agent can use the FreeCAD skill
to produce correct parametric CAD models from natural language specifications.

## Flow

1. **Agent** receives `TASK.md` as the user prompt, with the FreeCAD skill
   staged from `//skills/freecad` and bind-mounted into the container at `/skill`
2. **Agent** uses MCP tools (`exec`, `read_image`) to run FreeCAD commands
   inside a Docker container and inspect outputs
3. **Agent** produces a `.FCStd` file in the workspace
4. **Judge** (human or LLM) inspects the workspace files and transcript,
   fills out `CHECKLIST.md`

## Running

```bash
ANTHROPIC_API_KEY=sk-... bazel run //skills/freecad/eval:run_eval -- /tmp/eval-output
ANTHROPIC_API_KEY=sk-... bazel run //skills/freecad/eval:run_eval -- /tmp/eval-output --model claude-opus-4-6
```

Requires Docker (the FreeCAD container runs via the `exec` MCP server).

## Output

```
/tmp/eval-output/
├── transcript.jsonl    # Incremental JSONL log of all agent messages
├── metadata.json       # cost_usd, duration_s, turns, model, session_id
└── workspace/          # Agent's work files
    ├── baseplate.py    # Agent's script (if it wrote one)
    ├── baseplate.FCStd # The deliverable
    └── *.svg, *.png    # Any renders
```

## Task structure

Each task is a directory containing:

- `TASK.md` — the prompt given to the agent
- `CHECKLIST.md` — criteria the judge evaluates, with a scoring rubric
