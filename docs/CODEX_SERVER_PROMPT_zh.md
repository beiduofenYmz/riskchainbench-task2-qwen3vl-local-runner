# Server Codex 指令：本地 Qwen3-VL-8B-Instruct 跑 Task 2

你在一台 Linux GPU 服务器上工作。目标是在本机完成一次
RiskChainBench Task 2 的 Qwen3-VL-8B-Instruct 网页调查轨迹实验，并把完整
结果只保存到本地磁盘。

## 固定要求

1. 使用当前目录的 `upstream/`，不得编辑其任何文件；它固定为
   `task2-trajectory-v0.4.1` / commit
   `52b3e1e9abf4cc0f75b33a2bb427c59e5a88488b`。
2. 协议必须是 `riskchainbench-task2-trajectory-v0.4`，合同 SHA 必须是
   `52b2e3e4bbca46fe6f007bdc7b553fa0b2f1f923c82a5b775faef09350e9ff42`。
3. 数据仅从 `beiduofen/riskchainbench-task2-controlled-web-replay` 下载；
   不要访问已失效的原始数据链接，也不要混用其他数据版本。
4. 被测模型为 `Qwen/Qwen3-VL-8B-Instruct`，经本机 vLLM 的
   OpenAI-compatible `/v1/chat/completions` 服务调用。
5. 不得改变 Balanced-600 顺序、Prompt、codebook、截图规格、BrowserGym /
   Playwright harness、30 步预算或 600 秒预算。
6. 不得给模型外网搜索、域名信誉、Human Gold、Task 1 输出或隐藏 resolver。
   不得让模型做内联 Judge、违规分类或证据链总评分。
7. 矩阵运行时只允许 `--max-system-retry-rounds 2`。`MODEL_FAILURE`、拒答、
   坏 JSON、无效动作必须保留，不能改为重跑到成功。
8. 不得把任何 token、`.env`、数据、Docker 镜像或结果上传到 GitHub；结果
   必须留在本机 `runs/<run-id>/`。

## 执行步骤

1. 进入仓库。检查 Docker、`zstd`、Tesseract、Python 3.10、可用 GPU 和磁盘。
   缺少系统依赖时安装；缺少 ModelScope 登录或本地模型权重访问权限时，简洁报告
   阻塞原因，不要伪造结果。
2. 执行 `bash automation/setup_task2_runner.sh`。
3. 用操作者提供的凭据执行 `.venv-task2/bin/modelscope login`，再运行
   `bash automation/download_task2_from_backup.sh`。确认 metadata 验证为 `PASS`。
4. 创建权限为 0600 的 `.env`，至少写入：
   `LOCAL_OPENAI_BASE_URL=http://127.0.0.1:8000/v1`、
   `LOCAL_OPENAI_API_KEY=<本地服务密钥>`、
   `QWEN_MODEL=Qwen/Qwen3-VL-8B-Instruct`。
5. 在独立 vLLM/CUDA 环境运行 `automation/serve_qwen3vl_vllm.sh`。等待
   `http://127.0.0.1:8000/v1/models` 可用。确保 `--served-model-name` 与
   `.env` 中 `QWEN_MODEL` 一致。
6. 执行 `bash automation/launch_background_run.sh`。记录终端打印的 run 路径和
   service/PID。随后用 `bash automation/watch_progress.sh runs/<run-id>` 观察。
7. 自动流程必须依次通过 full hash、600 镜像还原、2 次多模态路由探针、1 条
   smoke、10 条 smoke、正式 600 条矩阵。任何一关未通过都停止，不要绕过。
8. 完成后读取 matrix 的 `summary.json` 与 `validation.json`，确认 validation
   为 `PASS`，并确认 600 个 case 目录都保留。用
   `bash automation/pack_results.sh runs/<run-id>` 生成本地校验压缩包。

## 汇报格式

只汇报：代码 commit、数据 release/contract 校验、模型服务地址与模型名、每个
门禁状态、矩阵 PASS/MODEL_FAILURE/SYSTEM_FAILURE 数量、run 根目录和压缩包路径。
不要声称 accuracy、F1、最终违规判断或 Judge 分数。
