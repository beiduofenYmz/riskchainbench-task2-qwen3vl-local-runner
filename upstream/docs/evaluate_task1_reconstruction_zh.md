# RiskChainBench Task 1 恢复评估教程

> 数据单位：Balanced-600 的 600 个网站，每站 6 个消息变体，共 3,600 条
> 输入：仅平台侧混淆消息，不访问网页或域名信誉服务
> 输出：恢复文本、意图与最多 5 个入口候选

## 1. 获取代码与受控数据

```bash
git clone https://github.com/mattheliu/riskchainbench-task1.git
cd riskchainbench-task1

# 二选一，不要混用不同 revision
modelscope download leonliuzx/riskchainbench-task1 \
  --repo-type dataset --local-dir data/task1

# 或
hf download leonliuzx/riskchainbench-task1 \
  --repo-type dataset --local-dir data/task1
```

Task 1 的两个数据仓库均为申请制。`model_visible/` 可交给被测模型；
`evaluator_only/` 只用于离线计分，不能拼进 prompt。先依据
`FILE_MANIFEST.jsonl` 校验文件大小和 SHA-256。

## 2. 安装

```bash
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements-task1.txt
```

密钥只通过进程环境或权限为 `0600` 且不提交 Git 的 env 文件传入。

## 3. 选择接口

libinfer-neo：

```bash
export LIBINFER_NEO_URL='<endpoint>'
export LIBINFER_SK='<secret>'
TRANSPORT=libinfer-neo
BASE_URL_ENV=LIBINFER_NEO_URL
API_KEY_ENV=LIBINFER_SK
```

其他直接厂商或 OpenAI Chat Completions 兼容接口：

```bash
export VENDOR_CHAT_BASE_URL='<provider-compatible-endpoint>'
export VENDOR_CHAT_API_KEY='<secret>'
TRANSPORT=openai-compatible
BASE_URL_ENV=VENDOR_CHAT_BASE_URL
API_KEY_ENV=VENDOR_CHAT_API_KEY
```

模型 ID 不受 allowlist 限制。`openai-compatible` 模式不发送
`libinfer-*`、`reasoning_effort`、厂商 thinking 开关或
`response_format`。原生 Anthropic Messages、Gemini `generateContent` 或
OpenAI Responses 接口需先使用保持消息语义不变的兼容适配器。

## 4. 路由预检

```bash
MODEL='provider-model-id'

python scripts/probe_multimodal_routes.py \
  --transport "$TRANSPORT" \
  --base-url-env "$BASE_URL_ENV" \
  --api-key-env "$API_KEY_ENV" \
  --models "$MODEL" \
  --probe-count 2 \
  --out runs/preflight/task1-route.json
```

论文评估对象是多模态模型，因此 Task 1 也使用更严格的两次图像能力探针。
报告必须为 `PASS_FIXED_MLLM_SELECTED`，且模型状态为
`PASS_MULTIMODAL_ROUTE`。

## 5. 单条 smoke

```bash
python scripts/run_task1_text_batch.py \
  --tasks data/task1/model_visible/task1_inputs.jsonl \
  --task-manifest data/task1/model_visible/task1_inputs_manifest.json \
  --task-schema schemas/obfuscated_reconstruction_task_v0.1.schema.json \
  --prediction-schema schemas/obfuscated_reconstruction_prediction_v0.1.schema.json \
  --prompt data/task1/spec/model_prompt.md \
  --route-probe runs/preflight/task1-route.json \
  --transport "$TRANSPORT" \
  --base-url-env "$BASE_URL_ENV" \
  --api-key-env "$API_KEY_ENV" \
  --model "$MODEL" \
  --source-limit 1 \
  --variant-indices 0 \
  --concurrency 1 \
  --out runs/task1-smoke

python scripts/validate_task1_text_batch.py \
  --run-dir runs/task1-smoke \
  --out runs/task1-smoke/validation.json
```

验证必须为 `PASS`。运行目录与配置指纹绑定；改变模型、接口、prompt、setting
或数据 revision 时必须使用新目录。

## 6. 正式 3,600 条

```bash
python scripts/run_task1_text_batch.py \
  --tasks data/task1/model_visible/task1_inputs.jsonl \
  --task-manifest data/task1/model_visible/task1_inputs_manifest.json \
  --task-schema schemas/obfuscated_reconstruction_task_v0.1.schema.json \
  --prediction-schema schemas/obfuscated_reconstruction_prediction_v0.1.schema.json \
  --prompt data/task1/spec/model_prompt.md \
  --route-probe runs/preflight/task1-route.json \
  --transport "$TRANSPORT" \
  --base-url-env "$BASE_URL_ENV" \
  --api-key-env "$API_KEY_ENV" \
  --model "$MODEL" \
  --source-limit 600 \
  --concurrency 4 \
  --progress-every 20 \
  --out "runs/task1-$MODEL"
```

Runner 会复用同一配置下已经通过的 task，只重试没有观察到模型输出的系统
失败。模型拒答、坏 JSON 和 schema 失败保留为模型失败，不通过无限重试抬高
成功率。

## 7. 离线验证与计分

```bash
python scripts/validate_task1_text_batch.py \
  --run-dir "runs/task1-$MODEL" \
  --out "runs/task1-$MODEL/validation.json"

python scripts/score_obfuscated_reconstruction.py \
  --dataset data/task1/evaluator_only/generated_sessions.jsonl \
  --predictions "runs/task1-$MODEL/predictions.jsonl" \
  --schema schemas/obfuscated_reconstruction_prediction_v0.1.schema.json \
  --bootstrap-replicates 1000 \
  --output "runs/task1-$MODEL/score.json"
```

报告 CER、消息 exact match、入口 exact match/Recall@k、strict success，并按
平台、混淆 family、强度和 6 个变体分层。只有 `v000` 进入后续端到端离线
组合；其余 5 个变体不触发 Task 2 浏览。

## 8. 常见错误

1. 把 `evaluator_only/generated_sessions.jsonl` 放入模型 prompt。
2. Task 1 运行时访问网页、DNS、域名信誉或黑名单。
3. 路由探针与正式运行使用不同 endpoint、模型 ID 或 response-model alias。
4. 修改 prompt 或数据后继续复用旧 run 目录。
5. 对模型拥有输出的拒答或坏 JSON自动重试。
6. 用 Gold 修复错误入口后再声称是模型恢复结果。
7. 把 3,600 条 Task 1 消息误写成 600 条。
