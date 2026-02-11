# GPT-OSS 20B Benchmark Results

Benchmark of OpenAI's gpt-oss-20b (the smaller variant) running locally via
llama.cpp on CPU-only hardware.

## Environment

- **CPU**: Intel (family 6, model 207), 16 cores, 1 thread/core
- **RAM**: 21 GiB total
- **GPU**: None (CPU-only inference)
- **OS**: Linux 4.4.0 (gVisor sandbox)

## Model

- **Model**: gpt-oss-20b (OpenAI, Apache 2.0)
- **Architecture**: Mixture of Experts (MoE), 20.91B total params, 3.6B active
- **Quantization**: Q4_K_M (unsloth/gpt-oss-20b-GGUF)
- **Size on disk**: 10.81 GiB
- **Runtime**: llama.cpp (commit 2cce9fd)

## Results

### Summary

| Metric                        | Tokens/sec |
| ----------------------------- | ---------: |
| **Prompt processing (pp512)** | ~63-73 t/s |
| **Text generation (tg128)**   | ~12-13 t/s |

### Detailed Results

| Test                      |     Tokens/sec |
| ------------------------- | -------------: |
| pp1 (1 token prompt)      | 20.44 +/- 0.32 |
| pp128                     | 68.15 +/- 4.02 |
| pp256                     | 70.55 +/- 3.84 |
| pp512                     | 63.53 +/- 0.41 |
| pp1024                    | 63.18 +/- 1.91 |
| tg64 (generate 64 tokens) | 13.92 +/- 2.14 |
| tg128                     | 11.60 +/- 1.69 |
| tg256                     | 13.61 +/- 0.38 |

- `pp` = prompt processing (prefill speed)
- `tg` = text generation (decode speed, the user-facing output rate)

### Key Takeaway

On this 16-core CPU-only setup with 21 GiB RAM, gpt-oss-20b at Q4_K_M
quantization generates text at roughly **12-14 tokens/second**. Prompt
processing runs at **63-73 tokens/second** depending on prompt length.

## Notes

- The "AMX is not ready to be used" warning indicates the CPU's AMX
  (Advanced Matrix Extensions) instructions were not available in this
  sandboxed environment, which would improve performance on bare metal.
- The MoE architecture means only 3.6B parameters are active per token,
  which is why performance is reasonable despite the 20.9B total parameter
  count.
- All quantizations of this model are similar in size (~11-12 GiB) because
  90%+ of parameters are MoE FFN weights that OpenAI post-trained with
  MXFP4 quantization.
