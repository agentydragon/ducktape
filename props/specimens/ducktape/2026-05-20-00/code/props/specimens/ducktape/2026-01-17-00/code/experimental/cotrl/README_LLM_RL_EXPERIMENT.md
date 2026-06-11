# LLM as RL Agent Experiment

This experiment tests language models' ability to learn from raw numerical feedback in RL environments.

## Key Features

- **No semantic context**: Environments presented as opaque numerical states/actions
- **Pure reward signal**: Only numerical rewards guide learning
- **Multiple models**: Tests gpt-4o-mini, gpt-4o, and o1-mini (all with temp=1.0)
- **Multiple environments**: From simple discrete (FrozenLake) to continuous (CartPole)

## Running the Experiment

1. **Set OpenAI API key**:

   ```bash
   export OPENAI_API_KEY=your_key_here
   ```

2. **Test setup** (quick validation):

   ```bash
   python oneoff__test_llm_rl_minimal.py
   ```

3. **Quick test** (verify all models work):

   ```bash
   python oneoff__llm_rl_quick_test.py
   ```

   - Tests all 3 models on 2 environments with minimal runs

4. **Run full experiment**:

   ```bash
   python oneoff__llm_rl_experiment.py
   ```

   - Runs 180 experiments total (3 models × 6 environments × 10 runs)
   - Parallelized with concurrency limit of 10 to avoid API overload
   - Each environment uses appropriate episode length (100-500 steps)
   - Shows progress as [current/total] for each experiment

## Output

- `llm_rl_results_TIMESTAMP.json` - Full experimental data with all steps
- `llm_rl_trajectories_TIMESTAMP.jsonl` - Trajectory data (states, actions, rewards)
- `llm_rl_results_TIMESTAMP.png/.pdf` - Visualization grid:
  - Rows: Models (gpt-4o-mini, gpt-4o, o1-mini)
  - Columns: Environments
  - Plots: Cumulative reward over timesteps with mean + confidence intervals

## Analyzing Results

To analyze action distributions and state visitation patterns:

```bash
python oneoff__analyze_trajectories.py llm_rl_trajectories_TIMESTAMP.jsonl
```

This generates:

- Console output with average rewards and action statistics
- `llm_rl_trajectories_TIMESTAMP_action_dist.png` - Action distribution heatmaps

## Environments Used

1. **FrozenLake-v1**: State=[0-15], 4 actions
2. **CliffWalking-v1**: State=[0-47], 4 actions
3. **CartPole-v1**: State=[4D float], 2 actions
4. **MountainCar-v0**: State=[2D float], 3 actions
5. **Blackjack-v1**: State=[3D tuple], 2 actions
6. **Acrobot-v1**: State=[6D float], 3 actions

## Prompt Template

```
You are in an environment. Your goal is to maximize cumulative reward.

State: [0.123, -0.456, 0.789, 0.234]
Last reward: -1.0
Available actions: 0, 1
Choose action:
```

Models only see raw numbers - no context about what they represent.
