# RiskChainBench Task 2 Local Qwen3-VL Runner

This private, code-only repository runs the frozen RiskChainBench Task 2
browser-trajectory protocol against a local OpenAI-compatible VLM endpoint.
It is prepared for `Qwen/Qwen3-VL-8B-Instruct` served by vLLM.

## What Is Included

- An unmodified snapshot of upstream `riskchainbench-task2` at
  `task2-trajectory-v0.4.1`, commit `52b3e1e9abf4cc0f75b33a2bb427c59e5a88488b`.
- Automation for environment setup, backup-data download, all required
  preflight gates, 1-case smoke, 10-case smoke, and the 600-case matrix.
- A vLLM launch helper for `Qwen/Qwen3-VL-8B-Instruct`.
- Background launch, progress watching, result packaging, and a Chinese prompt
  for a server-side Codex agent.

This repository intentionally excludes the 7 GB Task 2 release, Docker
materializations, model weights, run outputs, and all credentials.

## Frozen Protocol

- Protocol: `riskchainbench-task2-trajectory-v0.4`
- Contract SHA-256:
  `52b2e3e4bbca46fe6f007bdc7b553fa0b2f1f923c82a5b775faef09350e9ff42`
- Upstream compatibility tag: `task2-trajectory-v0.4.1`
- Model role: browser interaction and evidence collection only. It must not
  produce an inline risk label, final judgment, or evidence-chain score.

The outer automation never changes files under `upstream/`. Its scripts call
the official release verifier, runtime materializer, route probe, smoke runner,
matrix runner, and validator unchanged.

## Data Source

The download script uses the private backup dataset:

```text
beiduofen/riskchainbench-task2-controlled-web-replay
```

That backup was validated locally against the upstream release manifest:
600 Docker archives, 880 manifest payload files, and the Task 2 contract all
match. ModelScope adds `.gitattributes`, `README.md`, and `dataset_infos.json`
to newly created datasets; the download script removes only those three
platform-generated files before running the frozen verifier.

For an official leaderboard submission, retain the upstream-required source
revision in the experiment record or obtain maintainer approval for using this
byte-verified backup. The full local verifier must be `PASS` either way.

## Server Prerequisites

- Linux x86-64, Docker, `zstd`, and Tesseract.
- Python 3.10 for the frozen Task 2 runner.
- A CUDA/vLLM environment capable of serving Qwen3-VL-8B-Instruct.
- Disk for the Task 2 release, Docker runtime materialization, browser cache,
  and result screenshots. Keep data, runtime, and `runs/` on local server disk.

Install system packages on Ubuntu/Debian when needed:

```bash
sudo apt-get update
sudo apt-get install -y docker.io zstd tesseract-ocr
```

Ensure the running user can use Docker before starting the experiment.

## Quick Start

```bash
git clone https://github.com/beiduofen/riskchainbench-task2-qwen3vl-local-runner.git
cd riskchainbench-task2-qwen3vl-local-runner

bash automation/setup_task2_runner.sh
.venv-task2/bin/modelscope login
bash automation/download_task2_from_backup.sh

cp .env.example .env
chmod 600 .env
```

Edit `.env` so `LOCAL_OPENAI_BASE_URL`, `LOCAL_OPENAI_API_KEY`, and
`QWEN_MODEL` match the local vLLM service. Do not commit this file.

In a separate terminal inside the server's vLLM environment:

```bash
export VLLM_API_KEY='a-local-secret'
bash automation/serve_qwen3vl_vllm.sh
```

Then start the complete automated pipeline:

```bash
bash automation/launch_background_run.sh
```

The pipeline executes, in order:

1. metadata and full release verification;
2. 1-case and then 600-case mirror materialization;
3. multimodal route probe;
4. 1-case and 10-case smoke runs plus validation;
5. the official one-pass 600-case matrix, with at most two system-error retry
   rounds.

It stops on any preflight or smoke validation failure. It does not replace
`MODEL_FAILURE` with a later successful trajectory.

## Progress And Outputs

After the run starts, locate the printed `runs/<run-id>` path and run:

```bash
bash automation/watch_progress.sh runs/<run-id>
```

Complete artifacts remain local under `runs/<run-id>/`, including each case's
`case_result.json`, `trajectory.json`, `evidence_package.json`,
`judge_handoff.json`, screenshots, `model_calls.json`, `network_audit.json`,
plus matrix `summary.json` and `validation.json`.

To create a local transport archive after completion:

```bash
bash automation/pack_results.sh runs/<run-id>
```

## Server Codex

Use [docs/CODEX_SERVER_PROMPT_zh.md](docs/CODEX_SERVER_PROMPT_zh.md) as the
single prompt for a server-side Codex agent. It tells the agent to use this
repository and backup data, preserve the frozen runner, fail closed at every
gate, and leave all results on local disk.

## References

- Upstream Task 2 repository:
  <https://github.com/mattheliu/riskchainbench-task2>
- Qwen model card:
  <https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct>
- vLLM OpenAI-compatible server documentation:
  <https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/>
