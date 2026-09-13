# Which models are safe to hand tools to

Same 46 attacks, same undefended policy (`p48/s58`), same 18-case benign suite, one model at a time.

A model that never calls a tool scores zero on attacks and is useless as an agent. Those are marked **NO** under Usable, and their zero is not a safety result.

| Model | Attacks | Manipulated | Executed out of scope | Benign | False refusals | Usable |
| --- | --- | --- | --- | --- | --- | --- |
| `zai-org/GLM-5.2` | 46 | 65.2% | **26.1%** | 94.4% | 0.0% | yes |
| `Qwen/Qwen3-235B-A22B-Instruct-2507` | 46 | 71.7% | **23.9%** | 94.4% | 0.0% | yes |
| `moonshotai/Kimi-K2.6` | 46 | 65.2% | **23.9%** | 94.4% | 0.0% | yes |
| `deepseek-ai/DeepSeek-V3.1` | 46 | 69.6% | **21.7%** | 94.4% | 0.0% | yes |
| `meta-llama/Llama-3.1-8B-Instruct` | 46 | 63.0% | **21.7%** | 94.4% | 0.0% | yes |
| `openai/gpt-oss-120b` | 46 | 63.0% | **19.6%** | 100.0% | 0.0% | yes |
| `meta-llama/Llama-3.3-70B-Instruct` | 46 | 60.9% | **19.6%** | 94.4% | 0.0% | yes |
