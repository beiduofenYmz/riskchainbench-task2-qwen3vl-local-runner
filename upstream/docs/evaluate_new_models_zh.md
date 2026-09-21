# RiskChainBench 新模型评估入口

> 最近更新：2026-07-26
> 本页是稳定索引。Task 1 与 Task 2 分开运行、分开计分。

## 1. 数据与代码

| 任务 | GitHub | ModelScope | Hugging Face |
|---|---|---|---|
| Task 1 | `mattheliu/riskchainbench-task1` | `leonliuzx/riskchainbench-task1` | `leonliuzx/riskchainbench-task1` |
| Task 2 | `mattheliu/riskchainbench-task2` | `leonliuzx/riskchainbench-task2-controlled-web-replay` | `leonliuzx/riskchainbench-task2-controlled-web-replay` |

所有仓库均以各自的 `RELEASE_MANIFEST.json` 与 `FILE_MANIFEST.jsonl` 为准。
不要混拼 Hugging Face 与 ModelScope 的不同 revision。

## 2. Task 1

Task 1 评估混淆消息恢复。Balanced-600 每站有 6 个变体，共 3,600 条消息；
其中 `v000` 是端到端分析的主变体，其余 5 个用于恢复鲁棒性。

正式指标：

- 字符错误率 CER；
- 消息 exact match；
- 入口 exact match / Recall@k；
- strict success；
- 按混淆 family、强度与平台分层结果。

Task 1 输出冻结后，使用 `prepare_task2_handoff.py` 生成 fail-closed gate
manifest。该 manifest 不会传入 Task 2 浏览器，只用于最后的离线组合。

数据下载、`libinfer-neo` 与 OpenAI-compatible 接口、路由预检、单条 smoke、
3,600 条正式运行、断点续跑和离线计分见：

[`docs/evaluate_task1_reconstruction_zh.md`](evaluate_task1_reconstruction_zh.md)

## 3. Task 2

Task 2 对 Balanced-600 的 600 个网站采集浏览轨迹。每个被测模型、每个网站
只运行一次：

```text
实际 Task 2 轨迹数 = 模型数 × 600
```

被测模型只负责浏览、交互和固定证据。违规判断、违规类型及四维证据质量评分
后续统一交给一个冻结的独立多模态 Judge。

正式 Task 2 协议 ID 为 `riskchainbench-task2-trajectory-v0.4`，合同 SHA-256
为 `52b2e3e4bbca46fe6f007bdc7b553fa0b2f1f923c82a5b775faef09350e9ff42`。
运行前必须通过 `verify_task2_release.py`；Runner 只接受一个发布根目录，不能
单独替换 resolver、Prompt、codebook 或镜像清单。

完整命令、`libinfer-neo` 与直接厂商/OpenAI-compatible 接口、route probe、
runtime 还原、1/10 条 smoke、600 条矩阵、resume、进度查看、产物说明与常见
错误见：

[`docs/evaluate_task2_trajectories_zh.md`](evaluate_task2_trajectories_zh.md)

## 4. 两个任务如何组合

`reference_restoration` 与 `model_restoration` 是离线分析列，不是两次网页执行：

- reference：复用该模型已经采集的一条 Task 2 轨迹；
- model：Task 1 `v000` 入口正确时复用同一轨迹；
- Task 1 入口错误、缺失或拒答：记为 `NON_INVESTIGABLE`；
- 不允许用 Gold 修复，不再次启动浏览器。

使用：

```bash
python scripts/compose_task1_task2_end_to_end.py \
  --task1-handoff <handoff_manifest.json> \
  --task2-run <task2_model_run> \
  --out <composed_output>
```

## 5. 当前论文示例模型

当前内部矩阵示例为：

```text
gpt-5.4
claude-opus-4-8-kiro
kimi-k2.6
gemini-3.6-flash
```

它们不是 allowlist。其他研究者可以传入任意模型 ID，但必须先在同一 endpoint
上通过两次多模态路由探针，再通过 1 条和 10 条任务级 smoke。

## 6. 计分边界

Judge 与 Human Gold 未冻结前，只能发布：

- 轨迹完成率和失败类型；
- 协议有效率；
- 零外网审计；
- 动作数、耗时与重复动作诊断；
- 完整证据包与文件哈希。

不得提前发布 accuracy、F1、正式证据链总分或 leaderboard。
