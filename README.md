# AgentReliability-Platform

面向工具型 AI Agent 的自动化测试与回归门禁。平台使用固定提交的电商数据分析 Agent 真实工作流，在合成订单库上离线运行用例，输出结构化报告与逐步轨迹。M1 覆盖正常、纠错与结果错误判定；M2 将正式离线套件扩展到 20 条，覆盖数据正确性、多轮会话、执行失败恢复与安全前置拦截。M3 增加静态 HTML 报告、基线与候选对比，以及 `agent-regression` PR 检查。M4 增加显式联网的真实模型抽样：固定模型与目标提交，对 6 条用例各跑 3 次，独立报告判定、耗时、调用次数与提供方错误。平台化阶段增加 `quality` 闭环（静态扫描、故障复现、离线调查、Docker 补丁复测）、OpenJudge 语义评分、2×2 模型矩阵与 A.I.G MCP 扫描适配。

## 环境准备

需要 Python 3.12、uv 与 git。依赖由 `pyproject.toml` 和 `uv.lock` 锁定。

在项目根目录准备被测源码。已有目标 checkout 时，核对来源与提交后直接使用：

```sh
git clone --filter=blob:none --no-checkout https://github.com/super-ZXQ/ai-commerce-intelligence-platform.git .targets/commerce
git -C .targets/commerce checkout --detach 5e63127268dd45c4f526635dcc97d51fc5b41de8
uv sync --locked
```

CLI 运行前会校验 `target.lock.json` 中的仓库来源、完整提交 SHA，以及目标工作树没有已跟踪文件的未提交改动。依赖与目标源码准备完成后，离线执行不需要网络和模型凭据。

## 运行

```sh
uv run --locked pytest -q
uv run --locked agentreliability run --mode offline --target-dir .targets/commerce --out artifacts/m2
```

退出码约定：全部通过为 0，断言失败为 1，输入、执行环境或产物不可用为 2。

## 用例矩阵

正式套件共 20 条，固定数据包含 1 月、2 月与 3 月订单（3 月两笔同为 12），以及仅用于越权测试的 `private_notes` 表。

| 组 | 用例 | 覆盖重点 |
| --- | --- | --- |
| DATA | DATA-01 ~ DATA-08 | 月销售额、月份边界、聚合分组、计数、金额筛选、排序与空结果；3 月重复金额保留两行 |
| CONV | CONV-01 ~ CONV-04 | 同会话追问携带历史、模糊输入澄清、不同线程隔离、同一线程不同用户隔离 |
| REL | REL-01 ~ REL-04 | 执行错误后纠错、单次超时恢复、持续超时重试上限、非法 SQL 在执行前被拦截 |
| SEC | SEC-01 ~ SEC-04 | 直接提示注入、写操作、多语句绕过、越权读取私有表；执行器入口均为零调用 |

M1 基线用例 DATA-01 仍验证结果为 42 且执行一次；REL-01 仍验证 `error`、`success` 两次执行与纠错上下文。判定器负例在 `tests/` 中：去掉月份上界后 SQL 成功执行但得到 116（1 月至 3 月金额合计），判定为 `FAIL`，报告保留预期 42 与实际 116。

## 报告

每次运行写入指定 `--out` 目录：

- `report.json`：版本、运行模式、变体、目标仓库与提交、用例与数据 SHA-256、逐条断言及轨迹编号。
- `trace.jsonl`：每行一个事件，记录 `run_id`、`case_id`、试验与轮次、事件编号、来源、步骤、状态和耗时；模型事件附带其实际收到的会话历史。
- `report.html`：零依赖静态页面，展示摘要、逐条断言、关联轨迹、证据载荷和单例复现命令；所有输入与输出均经 HTML 转义。

M2 验收产物位于 `artifacts/m2/`。`.targets/` 与 `artifacts/` 不进入版本控制。

## 平台化质量闭环

`quality` 入口用同一条固定场景串起静态扫描、故障复现、离线调查、Docker 沙箱补丁复测与人工审核状态。

```sh
uv run --locked agentreliability quality --scenario orders-slow-query --out artifacts/platform/orders-slow-query
```

场景源码固定为 IncidentPilot `566639ea876eed171c705ad0c1a0d79276b8584b` 的 `reference/orders_api`，注入 `fault_002_slow_database_query`（模拟延迟 0.5 秒，非真实故障）。每次运行写入新目录 `runs/<UTC 时间>-<运行 ID>/`：

- `manifest.json`：`run_id`、场景与注入故障、目标仓库与完整 SHA、变体、四个子任务的状态/执行方式/产物/外部运行 ID、闭环结论与人工审核状态。
- `scan.json` / `scan.raw.json`：VibeCheck-AI 真实扫描的白名单字段与原始输出。
- `failure-assertion.json`：原版本在故障条件下的延迟断言、关联运行 ID 与支持证据 ID。
- `investigation.json`：IncidentPilot 真实运行 ID、工具调用、证据、诊断、补丁产物与测试结果。
- `sandbox-retest.json`：Docker 隔离工作区的基线 SHA、补丁摘要、故障条件断言与健康回归的命令和输出。
- `report.html`：按扫描 → 故障 → 诊断证据 → 补丁 → 复测 → 审核顺序展示，只使用脱敏字段并做 HTML 转义。

扫描器锁定在 VibeCheck-AI `3a0e22c7e49eb317cbecb80c4d7616668938300e`，通过其后端虚拟环境在独立进程执行真实 `app.scanner.sensitive.scan_directory`。调查由平台拥有的脚本在 IncidentPilot 虚拟环境内调用上游 `run_agent_workflow`，模型与工具为上游脚本化替身（`offline-scripted`）；Docker 复测为真实容器（`real-docker`）。预计根因只用于验证器比对，不注入被测 Agent。

闭环结论不是“所有子任务都通过”：`failure_assertion` 必须以 `FAIL` 命中注入故障，随后调查、补丁与两项复测通过，且人工审批保持 `NOT_RUN`，才输出 `PASS` 供人工审核。提交错配、缺失产物、超时、上游失败、未复现故障或 Docker 不可用都不会生成通过状态。

## 语义评分（OpenJudge）

语义评分独立展示，不参与离线硬门禁；硬门禁仍由确定性断言负责。样本冻结在 `benchmarks/semantic_groundedness/v1.json`，清单记录样本 SHA-256、校准/留出划分与标签来源。

```sh
uv run --locked agentreliability semantic --split calibration --mode offline --openjudge-dir .targets/openjudge --out artifacts/platform/semantic
```

OpenJudge 固定在 `b83f446b09821257da9c2070ca405fa71cc22690`，使用其独立虚拟环境在子进程运行 `ReasoningGroundednessGrader`。`offline` 模式注入词面覆盖测试替身，只验证适配器与计分链路（`execution_mode=test-double`）；`model` 模式读取 `OPENJUDGE_API_KEY`（或 `LLM_API_KEY`）、`OPENJUDGE_MODEL`（或 `LLM_MODEL`）与可选 `OPENJUDGE_BASE_URL`。缺失配置时写出 `NOT_RUN` 并以退出码 2 结束。留出集指标还要求 `manifest.json` 中 `labels_human_reviewed=true`，当前标签为项目编写、尚未人审，因此留出集命令按设计返回 `NOT_RUN`。

每次运行写入 `runs/<UTC 时间>-<运行 ID>/`：`samples.json`、`predictions.json`、`manifest.json` 与 `report.html`。报告分别给出清晰样本一致率、边界样本分布、误判与分歧例。

使用 `deepseek-flash` 对 10 条校准样本完成真实语义评分。清晰样本与项目标签一致 6/7，包含边界样本的偏好一致 8/10，执行错误 0。分歧之一是 cal-01，证据只有单个请求速率读数，诊断却称请求速率保持稳定。保留原始标签供人工复核。留出集 8 条待人工审核。

- 校准报告 `artifacts/openjudge-deepseek.DEbagn/runs/20261108T144502Z-d184e585/report.html`
- 留出集复核表 `artifacts/openjudge-deepseek.DEbagn/holdout-review.md`

## 2×2 矩阵

矩阵清单 `benchmarks/matrix/pawbench-2x2.json` 固定两个模型槽、`fixed`/`autonomous` 两种策略、两个故障场景、每组合 2 次重复，以及判定器和预算；参考 PawBench `0f794a8bb6c27aa9ee4091b2691fa30e4ed9cc8f` 的重复运行结构，本矩阵不是 PawBench 官方榜单。

```sh
uv run --locked agentreliability matrix --mode offline --out artifacts/platform/matrix
```

IncidentPilot 默认查找 `.targets/incidentpilot` 或 `~/Developer/GitHub/incidentpilot`，也可用 `--incidentpilot-dir` 指定锁定提交的 checkout。离线模式在 IncidentPilot 独立环境运行 `workflow_fake`（脚本化模型与合成工具，不联网、不启用沙箱），验证 16 次试验的调度、预算、汇总与失败展示。真实矩阵需要两个独立授权模型配置：`PAWBENCH_MODEL_A_API_KEY`/`_MODEL`/`_BASE_URL` 与 `PAWBENCH_MODEL_B_API_KEY`/`_MODEL`/`_BASE_URL`；缺失时 `--mode model` 写出 `NOT_RUN` 并以退出码 2 结束。

产物包含 `trials.json`、`manifest.json`（按模型 × 策略与场景汇总、真实矩阵待补状态）和 `report.html`。

已有一个授权模型时，可用 `matrix --mode model --model-slot model-a` 跑 1×2 策略对照。两种策略各跑两个故障场景，每个场景重复两次。报告会标记实际矩阵范围并保留原有 2×2 入口。为专用参考服务设置 `PAWBENCH_REFERENCE_ORDERS_API_URL`，可用 `PAWBENCH_REFERENCE_LOGS_PATH` 指定同一实例的日志目录。

首次联调可追加 `--smoke --timeout 420`，只在首个场景运行两种策略各一次，每次最多 180 秒。每条试验结束后立即落盘，总时限到达时保留已写入的结果。

DeepSeek Flash 的慢查询联调中，固定策略约 29 秒、调用工具 4 次，调查结束但根因错误。自主策略约 150 秒、调用工具 9 次，超过 8 次工具预算并进入人工介入。原始报告总状态为 `ERROR`，其中旧版逐试验统计把两条都记为通过。现已把真实试验超预算或需要人工介入归为 `ERROR`。本轮只覆盖一个模型和一个故障，完整 2×2 实测仍需第二个已配置模型。原始本地产物位于 `artifacts/matrix-deepseek-smoke/runs/20261108T235857Z-55b08010/`。

慢查询复跑使用专用 `PAWBENCH_REFERENCE_LOGS_PATH`。上游 MCP 的 `read_logs` 从最近一次故障注入开始截取日志，原先无法同时返回注入前后的请求。桥接层现在从当轮真实 HTTP 日志提取注入前 3 条和注入后 4 条 `/orders` 请求，将状态码和延迟作为 `controlled_reproduction` 观测写入当轮独立的 MCP 日志快照及矩阵报告。参考服务原日志由容器独占写入。观测不包含注入请求体、故障标签或金标。

固定策略复跑的基线约 2–3 ms，注入后约 503–507 ms。模型将延迟归因于应用侧注入；固定上游源码也在记录 `select_orders` 查询耗时后才调用 `time.sleep`。现有场景仍用 `database_performance` 作为期望类别，模型输出 `application_bug`，根因评分保持为错。两次运行都只覆盖同一个慢查询场景。自主策略用满 8 次工具调用后以 `INSUFFICIENT_EVIDENCE` 结束。真实试验现在要求 `INVESTIGATION_COMPLETE` 且在预算内才计为流程完成。按新规则重算同次实测得到 1 条流程完成、1 条错误、0 条根因正确。原始记录和重算报告保存在 `artifacts/matrix-deepseek-observed/runs/20261109T010554Z-23f8ba84/`。

后续使用 `orders-app-delay/v1` 校准真实慢查询场景。故障类型、0.5 秒注入参数、模型输入与预算沿用原场景，只有评测侧更新为 `application_bug`。根因判对同时要求结构化判定通过，以及引用的 `app.py` 证据覆盖 SQL 计时结束和处理器休眠分支。离线套件、其他场景和历史报告保留原判定。固定策略的源码工具默认只返回第 1–90 行，而延迟逻辑在第 160 行之后，校准复跑中，固定策略判对应用侧类别，缺少注入源码引用，严格根因仍未通过。自主策略触及 180 秒时限，未产出可评分诊断。报告位于 `artifacts/matrix-deepseek-calibrated-final/runs/20261109T014745Z-4129494d/report.html`。

固定策略随后加了真实 MCP 源码分页。每次源码读取最多合并三页，校验文件和行号连续，每个后端请求都计入原有工具预算。DeepSeek Flash 的同场景复跑中，固定策略 29 秒完成，4 次逻辑工具调用对应 6 次 MCP 请求，读取了 `app.py` 第 1–255 行，类别和注入源码引用均命中。自主策略约 98 秒完成，7 次逻辑工具调用，类别命中但注入源码引用未命中。两条流程均完成，严格根因通过 1 条。报告位于 `artifacts/matrix-deepseek-paged/runs/20261109T020909Z-d976e6af/report.html`。

源码引用审计将当轮成功工具调用的页码与通过哈希、运行编号和工具调用校验的证据 ID 关联，再检查最终根因是否引用该证据。报告只保留页码、文件名、工具调用和证据编号，以及命中状态。单次审计复跑中，固定策略命中并引用了注入代码。自主策略读到了 `app.py` 第 91–172 行，覆盖休眠分支，但最终以 `INSUFFICIENT_EVIDENCE` 结束，未产出根因引用。报告位于 `artifacts/matrix-deepseek-citation-audit/runs/20261109T023055Z-9387f3d6/report.html`。

## A.I.G MCP 扫描

```sh
export AIG_BASE_URL=http://localhost:8088
uv run --locked agentreliability aig --out artifacts/platform/aig-real
```

A.I.G 固定在 `76146bf479b2fedb34f7acb7f536aa37b1f8ba74`，流程为上传白名单 ZIP → 创建 `mcp_scan` 任务 → 有界轮询状态 → 拉取结果并统一展示发现。样例位于 `benchmarks/aig_samples/synthetic_mcp_server`，只含合成源码，上传时会跳过隐藏文件与非白名单后缀。实例地址缺失或不可达时写出 `NOT_RUN`；轮询超时、任务失败、统一响应 `status=1` 或结果解析失败都会写出 `ERROR`，不会生成通过状态。电商 SEC 断言仍由既有确定性套件独立负责。

A.I.G 4.6.9 已用本地官方容器和 DeepSeek 模型转发服务实跑。任务状态到达 `done`，报告记录合成样例中的命令注入和路径穿越两项发现。初次调用暴露了服务端使用 `todo/doing/done/error/terminated` 的兼容问题，适配后用 CLI 复跑通过。[腾讯朱雀实验室 A.I.G](https://github.com/Tencent/AI-Infra-Guard)

实例已有默认模型时可直接运行。用显式模型时，在本地终端一并配置 `AIG_MODEL`、`AIG_MODEL_BASE_URL` 和 `AIG_MODEL_TOKEN`，并将 `AIG_BASE_URL` 指向本地回环地址。本次只给 A.I.G 临时转发令牌，DeepSeek 密钥保留在本地。

- 真实扫描报告 `artifacts/aig-v469-final/runs/20261108T231826Z-e18c0cd7/report.html`

## 平台化阶段报告

完整阶段报告（逐项状态、命令、退出码、负向证明与待补验清单）：

- `artifacts/platform/phase-report/report.html`
- `artifacts/platform/verification.json`

## 发布前验收

发布前验收的可执行项已完成验证，证据统一放在 `artifacts/release-acceptance/`，历史证据保持原样：

- 验收报告：`artifacts/release-acceptance/report.html`
- 逐项证据：`artifacts/release-acceptance/item1` 至 `item5` 下的 `verification.json`
- 平台阶段总索引：`artifacts/platform/verification.json`

复跑核心项：

```sh
uv run --locked pytest -q
uv run --locked agentreliability run --mode offline --target-dir .targets/commerce --out artifacts/release-acceptance/item1/offline
uv run --locked agentreliability quality --scenario orders-slow-query --incidentpilot-dir ~/Developer/GitHub/incidentpilot --vibecheck-dir ~/Developer/GitHub/vibecheck-ai --out artifacts/release-acceptance/item2
uv run --locked agentreliability matrix --mode offline --incidentpilot-dir ~/Developer/GitHub/incidentpilot --out artifacts/release-acceptance/item3
```

质量闭环现在分四阶段呈现：调查流程完成、根因与证据验证、补丁复测、人工审核。诊断与金标不一致或证据不足时，总判定为 `REVIEW_REQUIRED` 并以退出码 1 结束，已验证的 Docker 补丁复测结果单独保留为 `PASS`；金标只用于判定器，不注入被测 Agent。

真实模型与服务实测需要本机配置：`LLM_*`（电商套件 6 条 × 3 次）、`OPENJUDGE_*` 与人工复核标签（语义评分）、`PAWBENCH_MODEL_A_*`/`PAWBENCH_MODEL_B_*`（真实 2×2 矩阵）、`AIG_BASE_URL`（A.I.G 扫描）。缺项与复测命令见验收报告中的待补验清单，未把离线或替身结果写入真实服务结论。

## 发布修复：三类误报边界（2026-11-08）

本轮修复真实矩阵、根因验证与 A.I.G 上传三处误报，证据在新目录 `artifacts/release-fix-20261108/`，历史 `artifacts/release-acceptance/` 原样保留：

1. **真实矩阵**：`matrix --mode model` 只走真实模型执行路径（`tier=real_llm_investigation`、`tool_backend=mcp`），两个模型槽分别读取 `PAWBENCH_MODEL_A_*` / `PAWBENCH_MODEL_B_*`。发布真实结果前逐条校验执行来源、模型标识与 provider 元数据；任一条来自脚本化替身即 `ERROR`（`matrix_real_result_invalid`），真实执行器或 reference API / MCP 环境不可用时返回 `NOT_RUN` 且退出码 2，不再产出 `real-service PASS`。
2. **根因验证**：判定器核验根因 `verified` 状态、订单慢查询场景的明确故障机制，以及实际引用的源码是否显示注入分支执行延迟；缺引用、引用无关证据、因果事实错误均判 `REVIEW_REQUIRED` 并退出 1。桥接层不再在根因缺引用时用本轮全部证据补齐；Docker 补丁复测结果仍独立记录为 `PASS`。
3. **A.I.G 上传**：`build_source_zip` 拒绝符号链接，并校验每个入包文件解析后的真实路径位于样例根目录内；目录外内容既不会进入 ZIP，也不会传给上传接口。

真实矩阵使用专用本地 reference API，地址需为 HTTP 回环地址（默认 `http://127.0.0.1:8001`）。每次试验开始和结束都确认故障重置。子进程超时后由父进程补做重置。清理失败会结束矩阵并报错。A.I.G 收到 HTTP 失败响应时记录状态码并返回 `ERROR`，连接失败则返回 `NOT_RUN`。

离线负例集中在 `tests/test_release_boundaries.py`（无需真实模型凭据与外部服务），已接入 PR 工作流的 `agent-regression` 检查，原有 20 条离线门禁保留。

本轮实际复跑：

```sh
uv run --locked pytest -q
uv run --locked agentreliability run --mode offline --target-dir .targets/commerce --out artifacts/release-fix-20261108/item1/offline
uv run --locked agentreliability quality --scenario orders-slow-query --incidentpilot-dir ~/Developer/GitHub/incidentpilot --vibecheck-dir ~/Developer/GitHub/vibecheck-ai --out artifacts/release-fix-20261108/item2
uv run --locked agentreliability matrix --mode offline --incidentpilot-dir ~/Developer/GitHub/incidentpilot --out artifacts/release-fix-20261108/item3
```

结果：pytest 99 通过（退出 0）；离线 20/20 通过（退出 0）；订单闭环总判定 `REVIEW_REQUIRED`（退出 1），根因类别与关键因果事实均与金标不一致，Docker 补丁复测独立为 `PASS`；离线矩阵 16/16 流程完成（金标根因正确 0，差异追溯到脚本化决策输出）；真实矩阵缺配置与占位配置分别返回 `NOT_RUN`（退出 2），均无试验产物。

真实项待补验（本机未配置，未伪造结果）：

| 项目 | 本轮状态 | 复跑命令 |
| --- | --- | --- |
| 电商真实模型 6 条 × 3 次 | `NOT_RUN`（缺 `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`） | `LLM_API_KEY=... LLM_BASE_URL=... LLM_MODEL=... uv run --locked agentreliability run --mode live --allow-network --repeat 3 --target-dir .targets/commerce --out artifacts/release-fix-20261108/item4/live` |
| 真实 2×2 矩阵 | `NOT_RUN`（缺 `PAWBENCH_MODEL_A_*`/`PAWBENCH_MODEL_B_*`，或 reference orders API 未启动） | `PAWBENCH_MODEL_A_API_KEY=... PAWBENCH_MODEL_A_MODEL=... PAWBENCH_MODEL_A_BASE_URL=... PAWBENCH_MODEL_B_API_KEY=... PAWBENCH_MODEL_B_MODEL=... PAWBENCH_MODEL_B_BASE_URL=... uv run --locked agentreliability matrix --mode model --incidentpilot-dir ~/Developer/GitHub/incidentpilot --out artifacts/release-fix-20261108/item3/model` |
| A.I.G 扫描 | `NOT_RUN`（缺 `AIG_BASE_URL`） | `AIG_BASE_URL=http://localhost:8088 uv run --locked agentreliability aig --out artifacts/release-fix-20261108/item5/aig` |
| 语义评分人工复核标签 | `NOT_RUN` | 见 `docs/DEVELOPMENT_PROMPT.md` 的待补验清单 |

发布候选报告：`artifacts/release-fix-20261108/report.html`；脱敏证据包：`artifacts/release-fix-20261108/evidence-bundle/`（自包含相对链接，复制到新目录后仍可打开）。

## 对比与红绿复现

先用默认行为跑健康基线，再用演示变体关闭 SQL 失败后的重试，最后恢复默认行为重跑：

```sh
uv run --locked agentreliability run --mode offline --target-dir .targets/commerce --out artifacts/m3/healthy
uv run --locked agentreliability run --mode offline --target-dir .targets/commerce --demo-fault disable-sql-retry --out artifacts/m3/injected
uv run --locked agentreliability compare artifacts/m3/healthy/report.json artifacts/m3/injected/report.json --out artifacts/m3/comparison
uv run --locked agentreliability run --mode offline --target-dir .targets/commerce --out artifacts/m3/restored
```

演示变体只覆盖 `AgentRuntime._after_execution`，SQL 执行失败后直接进入结果合成，因此 REL-01、REL-02、REL-03 会因缺少第二次模型调用和第二次执行而失败。默认行为恢复后 20 条全部通过。对比报告把这三条列为新增失败，并保留候选执行与失败断言；恢复版本的对比显示这三条被修复，退出码为 0。

比较前先校验报告格式、判定器版本、运行模式、用例与数据校验和；不一致时输出 `NOT_COMPARABLE` 并退出 2。单条复现使用 `--case <用例 ID>`。

- `comparison.json`：可比较性、逐用例变化分类（新增失败、修复、环境错误、缺失用例）和证据。
- `comparison.html`：同一份对比的可读页面，包含基线/候选执行与失败断言。

## GitHub 合并检查

`.github/workflows/agent-regression.yml` 定义 `agent-regression` 检查，在 PR 到 `main` 时执行。检查先运行 `tests/test_release_boundaries.py` 的三类离线负例（不需要模型凭据或外部服务），再检出完整历史，从 PR 基准提交读取 `target.lock.json`，用候选工作树中的锁分别准备目标 checkout，运行同一套 20 条用例并执行 `compare`。缺失基准锁、候选未覆盖 20 个唯一用例 ID、断言失败或环境错误都会让检查失败；失败时仍上传报告、轨迹、日志和 `ci-summary.json`。

将检查设为分支保护规则：在仓库 Settings → Branches 中为 `main` 启用规则，并要求 `agent-regression` 通过后才能合并。

演示 PR [#1](https://github.com/super-ZXQ/AgentReliability-Platform/pull/1) 记录了远端红绿结果。红灯运行给候选注入演示变体，`agent-regression` 失败（候选退出 1、对比退出 1），20 个唯一用例 ID 完整覆盖，报告与对比产物仍上传：[运行 42461841558](https://github.com/super-ZXQ/AgentReliability-Platform/actions/runs/42461841558)。同一分支移除演示参数后恢复绿灯（基线、候选、对比均退出 0，20 条保持通过）：[运行 42462245235](https://github.com/super-ZXQ/AgentReliability-Platform/actions/runs/42462245235)。`--candidate-demo-fault` 仅用于该演示 PR，正式门禁不传此参数。

本地验收在 `artifacts/m3/`：健康与恢复两次运行退出 0，注入运行退出 1，对比产出 REL-01、REL-02、REL-03 三条新增失败。远端 CI 的实际运行状态以对应 PR 的检查记录为准，两者分别记录。

## 真实模型抽样

真实模型模式必须显式开启联网，并从本地环境变量读取配置；报告只保存模型名与提供方主机，不保存密钥或完整地址。

```sh
export LLM_API_KEY=...
export LLM_BASE_URL=https://<提供方>/v1
export LLM_MODEL=<固定模型名>
uv run --locked agentreliability run --mode live --allow-network --repeat 3 --target-dir .targets/commerce --out artifacts/m4/live
```

固定用例为 DATA-01、DATA-02、DATA-03、DATA-06、CONV-01、SEC-01，每条独立运行 3 次，共 18 次任务；每次试验重置会话与只读 SQLite 数据。缺失任一环境变量、未传 `--allow-network` 或目标 checkout 不可用时，命令写出 18 条 `NOT_RUN` 报告并以退出码 2 结束，不发起任何模型请求。全部通过时退出 0，业务断言失败退出 1，提供方连接、超时或限流等错误计入 `ERROR`。

`report.json` 的 `live` 字段记录模型、提供方主机、重复次数与缺失环境；`case_summaries` 按用例汇总试验数、通过/失败/错误/未执行、平均与最大耗时、模型调用次数、重试次数、提供方错误数和 token 用量。真实报告与离线报告分开存放、分开判定。

本地实测使用 DeepSeek V4.1 Flash（`deepseek-flash`）和固定电商 Agent 提交。首轮 12/18 通过，其余 6 条集中在 DATA-02 和 DATA-06，原因是结果包含额外的正常业务列。修订后的 `live-result-fields-v2` 按用例要求的字段比对，保留行数和排名顺序断言。使用同一组用例重新实跑后，18/18 通过，0 失败、0 错误、0 未执行。SEC-01 的三次通过来自应用层前置拦截，模型调用为零。

- 首轮报告 `artifacts/deepseek-live.FjX7Am/report.html`
- 复跑报告 `artifacts/deepseek-live-v2.0wecSG/report.html`

## 三分钟演示

1. 红灯：演示 PR 给候选注入缺失重试，`agent-regression` 检查失败（[运行 42461841558](https://github.com/super-ZXQ/AgentReliability-Platform/actions/runs/42461841558)）。打开 `artifacts/m3/comparison/comparison.html`，REL-01 被列为新增失败，候选执行只有一次 `error`。
2. 轨迹定位：在 `artifacts/m3/injected/trace.jsonl` 中，REL-01 的 `sql_port` 在 `seq 167` 报错后，`seq 169` 直接进入 `agent` 的 `synthesize`；健康版在 `seq 167` 之后有 `seq 169` 的第二次 `model.generate_sql`，其 `previous_error` 为 `OperationalError：执行失败`，随后 `seq 172` 的 `sql_port` 成功。差异字段是 `executions`（期望两次、实际一次）与 `retry_context`（缺少第二次模型调用）。
3. 绿灯：同一分支移除演示参数后检查通过（[运行 42462245235](https://github.com/super-ZXQ/AgentReliability-Platform/actions/runs/42462245235)），本地对照显示 REL-01、REL-02、REL-03 恢复，20 条保持通过。
4. 真实模型：打开复跑报告，查看 18 次任务的判定、耗时、模型调用次数和提供方错误。用两轮报告解释字段投影导致的误报及其验证过程。
