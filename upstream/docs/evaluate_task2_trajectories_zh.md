# RiskChainBench Task 2 轨迹评估教程

> 正式协议：`riskchainbench-task2-trajectory-v0.4`
> Task 2 合同 SHA-256：`52b2e3e4bbca46fe6f007bdc7b553fa0b2f1f923c82a5b775faef09350e9ff42`
> 数据单位：Balanced-600 的 600 个网站
> 被测模型职责：浏览、交互、收集证据并结束调查
> 最终判断：后续由一个冻结的独立多模态 Judge 统一完成

## 1. 先明确运行次数

每个模型对每个网站只运行一次浏览器：

```text
实际轨迹数 = 模型数 × 600
```

四个模型对应 `4 × 600 = 2,400` 条实际轨迹。Task 1 不作为 Task 2
浏览器输入；`reference_restoration` 和 `model_restoration` 是后续离线分析列，
不会触发第二次浏览。

正式 Runner 不提供内联 Judge 参数。若某份脚本要求被测模型在浏览结束后自行
分类，它不是本协议。

## 2. 获取代码与数据

- GitHub：<https://github.com/mattheliu/riskchainbench-task2>
- ModelScope：<https://modelscope.cn/datasets/leonliuzx/riskchainbench-task2-controlled-web-replay>
- Hugging Face：<https://huggingface.co/datasets/leonliuzx/riskchainbench-task2-controlled-web-replay>

正式冻结位置如下：

- GitHub tag：`task2-trajectory-v0.4.1`
- ModelScope revision：`eeaf346fb393f50e6f7e9bb6ce128d5949d6d425`
- Hugging Face revision：`01dbca9f04063ff5faf3afa7c4bc615231de4135`

`v0.4.1` 是 runner/validator 的兼容性修订：普通 BrowserGym/Playwright
动作错误会作为可审计的动作结果留在轨迹中，并允许模型继续调查；它不修改
Balanced-600、镜像、Prompt、合同或评测口径。

先固定代码版本：

```bash
git clone https://github.com/mattheliu/riskchainbench-task2.git
cd riskchainbench-task2
git checkout task2-trajectory-v0.4.1
```

ModelScope 和 Hugging Face 提供同哈希的完整受控评估包，访问权限以平台页面
为准。选择一个作为主下载源，不要混拼两个 revision：

```bash
# 方案 A：ModelScope
modelscope download leonliuzx/riskchainbench-task2-controlled-web-replay \
  --repo-type dataset \
  --revision eeaf346fb393f50e6f7e9bb6ce128d5949d6d425 \
  --local-dir data/task2

# 方案 B：Hugging Face
hf download leonliuzx/riskchainbench-task2-controlled-web-replay \
  --repo-type dataset \
  --revision 01dbca9f04063ff5faf3afa7c4bc615231de4135 \
  --local-dir data/task2
```

必须下载同一个不可变 revision。不要先从一个平台下载 metadata，再从另一个
平台补镜像。下载后先做 metadata 门禁：

```bash
python scripts/verify_task2_release.py \
  --task2-release data/task2 \
  --level metadata \
  --out runs/preflight/release-metadata.json
```

首次正式评测还必须完成全量镜像哈希门禁（约 7 GB，耗时较长）：

```bash
python scripts/verify_task2_release.py \
  --task2-release data/task2 \
  --level full \
  --out runs/preflight/release-full.json
```

两份报告必须均为 `PASS`。正式目录只应包含：

```text
model_visible/
evaluator_only/
spec/
docker_images/
runtime_supplement/
task2_contract.json
handoff_contract.json
FILE_MANIFEST.jsonl
RELEASE_MANIFEST.json
```

`task2_contract.json` 必须匹配上述固定 SHA，并声明 600 个 case、每模型每站
一次网页执行、Task 1 不可见、内联 Judge 禁止和统一 Judge 延后执行。任何旧版
“双条件各跑一次”的教程或脚本都属于 deprecated，不得用于正式结果。

## 3. 环境

```bash
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements-task2.txt
python -m playwright install chromium
sudo apt-get install -y tesseract-ocr zstd
```

需要 Linux x86-64、Chromium、Playwright、BrowserGym、Tesseract、`zstd`
和足够容纳 600 个本地镜像的磁盘。正式锁定版本为
`browsergym-core==0.14.3` 和 `playwright==1.44.0`；版本不符时 runner 会在
调用模型前失败。若 Tesseract 不在 `PATH`，通过 `--tesseract-root` 指向包含
`usr/bin/tesseract`、动态库和 `tessdata` 的便携根目录。

密钥只能放在权限为 `0600` 的环境文件或进程环境中。不得放进命令、Git、
JSON、截图或模型 prompt。

## 4. 选择接口

### 4.1 libinfer-neo

```bash
export LIBINFER_NEO_URL='<endpoint>'
export LIBINFER_SK='<secret>'
TRANSPORT=libinfer-neo
BASE_URL_ENV=LIBINFER_NEO_URL
API_KEY_ENV=LIBINFER_SK
```

### 4.2 直接厂商或 OpenAI-compatible 接口

接口必须支持 `/v1/chat/completions`、`image_url` 数据 URL 和 JSON 输出：

```bash
export OPENAI_BASE_URL='<provider-compatible-endpoint>'
export OPENAI_API_KEY='<secret>'
TRANSPORT=openai-compatible
BASE_URL_ENV=OPENAI_BASE_URL
API_KEY_ENV=OPENAI_API_KEY
```

`openai-compatible` 模式不会发送任何 `libinfer-*` 扩展字段。OneAPI 不属于
本协议支持的 transport。该模式采用保守请求配置：依靠 prompt 约束 JSON，
不发送 `reasoning_effort`、厂商 thinking 开关或 `response_format`。模型返回
仍由同一个严格 JSON validator 校验。

模型名称由调用者传入，不存在代码内 allowlist；但每个名称必须先通过同一
endpoint 的多模态路由探针。

也可以使用自定义环境变量名：

```bash
export VENDOR_CHAT_BASE_URL='<provider-compatible-endpoint>'
export VENDOR_CHAT_API_KEY='<secret>'
TRANSPORT=openai-compatible
BASE_URL_ENV=VENDOR_CHAT_BASE_URL
API_KEY_ENV=VENDOR_CHAT_API_KEY
```

v0.4 直接支持的是 OpenAI Chat Completions 兼容协议。原生
Anthropic Messages、Gemini `generateContent`、OpenAI Responses 等不同 wire
format 必须先接到不改写图像和消息语义的兼容适配器；不能在 runner 内临时
改 prompt、降级成纯文本或静默换模型。适配后仍需执行下面的路由探针。

## 5. 多模态路由探针

```bash
MODELS='model-a,model-b'

python scripts/probe_multimodal_routes.py \
  --transport "$TRANSPORT" \
  --base-url-env "$BASE_URL_ENV" \
  --api-key-env "$API_KEY_ENV" \
  --models "$MODELS" \
  --priority "$MODELS" \
  --probe-count 2 \
  --out runs/preflight/routes.json
```

只有报告整体为 `PASS_FIXED_MLLM_SELECTED`，且每个模型均为
`PASS_MULTIMODAL_ROUTE`，才能继续。探针使用本地生成的无害图片，不产生任务
得分。

## 6. 还原 600 个本地环境

先还原 1 条，再还原 600 条：

```bash
python scripts/materialize_task2_runtime.py \
  --task2-release data/task2 \
  --runtime-root runtime \
  --case-ref CASEd150dbe314a6c11bc1fa \
  --workers 1 \
  --report runs/preflight/materialize-smoke.json

python scripts/materialize_task2_runtime.py \
  --task2-release data/task2 \
  --runtime-root runtime \
  --workers 4 \
  --report runs/preflight/materialize-600.json \
  --replace
```

`materialize-600.json` 必须为 `PASS` 且 `materialized_case_count=600`。
默认从 `data/task2/runtime_supplement/` 读取冻结补充包，不需要再从源码仓库
寻找 observation plan 或 stateful profile。

## 7. 1 条和 10 条 smoke

单条：

```bash
python scripts/run_task2_autonomous_mllm_batch.py \
  --task2-release data/task2 \
  --route-probe runs/preflight/routes.json \
  --transport "$TRANSPORT" \
  --base-url-env "$BASE_URL_ENV" \
  --api-key-env "$API_KEY_ENV" \
  --runtime-root runtime \
  --runtime-materialization-report runs/preflight/materialize-600.json \
  --model model-a \
  --case-ref CASEd150dbe314a6c11bc1fa \
  --workers 1 \
  --out runs/smoke-1
```

10 条时去掉 `--case-ref`，添加 `--limit 10`。通过后执行：

```bash
python scripts/validate_task2_autonomous_mllm_batch.py \
  --run runs/smoke-1 \
  --expected-transport "$TRANSPORT" \
  --expected-task2-contract-sha256 \
    52b2e3e4bbca46fe6f007bdc7b553fa0b2f1f923c82a5b775faef09350e9ff42 \
  --out runs/smoke-1/validation.json
```

验证必须为 `PASS`，并确认：

- `judgment_mode=DEFERRED_FIXED_EXTERNAL_JUDGE`；
- 所有模型调用 phase 都是 `web_action`；
- `judgment=null`；
- `judgment_status=PENDING_FIXED_EXTERNAL_JUDGE`；
- 零外网审计通过；
- `judge_handoff.json` 和所有截图哈希可回读。

## 8. 正式 600 条矩阵

```bash
python scripts/run_task2_trajectory_matrix.py \
  --models "$MODELS" \
  --transport "$TRANSPORT" \
  --base-url-env "$BASE_URL_ENV" \
  --api-key-env "$API_KEY_ENV" \
  --task2-release data/task2 \
  --route-probe runs/preflight/routes.json \
  --runtime-root runtime \
  --runtime-materialization-report runs/preflight/materialize-600.json \
  --parallel-models 2 \
  --case-workers 2 \
  --max-system-retry-rounds 2 \
  --out runs/task2-matrix
```

查看一次进度：

```bash
python scripts/watch_task2_trajectory_matrix.py --run runs/task2-matrix
```

持续查看：

```bash
python scripts/watch_task2_trajectory_matrix.py \
  --run runs/task2-matrix --watch --interval 60
```

重新执行同一矩阵命令会进入严格 resume：只复用哈希完整的 `PASS` 或
`MODEL_FAILURE` case；只重跑系统失败和未完成 case。合同、600 条顺序、代码、
Prompt、codebook、预算、浏览器版本或输入哈希变化时拒绝续跑。

## 9. 每条轨迹的产物

```text
cases/<CASE_REF>/
  case_result.json
  trajectory.json
  evidence_package.json
  judge_handoff.json
  network_audit.json
  runtime_attestation.json
  model_calls.json
  model_calls/
  model_visible/
  redaction_audits/
```

`judge_handoff.json` 是后续统一 Judge 的入口，包含候选证据截图、完整轨迹、
动作账本、codebook 和 prompt 哈希，但不包含 Human Gold、Task 1 预测、原始
域名、信誉信息、隐藏 selector 或测试 fixture。

## 10. 失败记账

- HTTP 408/429/5xx、浏览器崩溃、case watchdog：系统失败，可有限重试。
- 单个元素被遮挡、不可见、定位歧义、脱离 DOM 或动作超时：记录稳定的
  `ACTION_ERROR` 与错误码，模型继续调查；不得强制点击或伪造页面变化。
- 拒答、坏 JSON、无效动作、提前停止、30 步仍未完成：模型失败，保留终态，
  不自动重试。
- 600 秒是上限，不是目标时长；模型完成后应主动 `stop`，runner 会立即退出。
- 任何外网请求尝试都使零外网审计失败。

## 11. Task 1 的离线组合

Task 1 单独运行并单独计分。需要论文中的两列分析时：

```bash
python scripts/compose_task1_task2_end_to_end.py \
  --task1-handoff runs/task1/model-a/handoff_manifest.json \
  --task2-run runs/task2-matrix/models/model-a/run \
  --out runs/composed/model-a
```

授权的 Task 1 `v000` 复用同一 Task 2 轨迹；入口错误或缺失记为
`NON_INVESTIGABLE`。不使用 Gold 修复，也不再启动浏览器。

## 12. 统一 Judge

轨迹矩阵完成后，再冻结一个独立多模态 Judge 的：

- 精确模型 ID 与响应模型 ID；
- system prompt 和输出 schema；
- codebook；
- 解码参数；
- 图像数量和字节预算；
- 四维评分量表；
- 人类对齐与受控扰动校准版本。

冻结前不得把规则占位分或被测模型自评当作正式证据链分，也不得报告正式
accuracy、F1 或 leaderboard。

## 13. 常见错误

1. 使用旧双条件矩阵脚本，导致同一网站重复浏览两次。
2. 把 Task 1 predictions 传入 Task 2 runner。
3. 使用仍含 `judge_system` 或 `--run-inline-judge` 的旧 Prompt/Runner。
4. 模型 ID 未经过当前 endpoint 的多模态探针。
5. 把密钥写进命令或 `.env` 后提交 Git。
6. 混用 ModelScope 与 Hugging Face 的不同 revision。
7. 手改 runtime、resolver、prompt 或 codebook 后继续 resume。
8. 把系统失败当模型失败，或重试模型拒答以抬高完成率。
9. 将 Human Gold、source label、域名信誉或隐藏交互协议暴露给模型。
10. Judge 未冻结和校准就发布正式证据质量分。

## 14. 团队对齐清单

每位评测者开始正式运行前，必须把以下值发回协调人并保存到运行目录：

```text
task2_protocol_id
task2_contract_sha256
release_sha256
ordered_case_refs_sha256
route_probe_sha256
runtime_materialization_report_sha256
model_id
resolved_model_id(s)
transport
BrowserGym / Playwright version
```

协调人只接收 `verify_task2_release=PASS`、1 条 smoke=PASS、10 条 smoke=PASS
且上述值一致的运行。`parallel-models` 与 `case-workers` 可以按机器资源调整；
它们不改变 Prompt、预算、顺序、证据格式或计分口径。
