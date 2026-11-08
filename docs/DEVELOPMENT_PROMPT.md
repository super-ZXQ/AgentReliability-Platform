# 开发提示词

你是 AgentReliability Platform 的开发代理。请在当前仓库直接完成本轮里程碑，写出可运行的代码并执行验收。

## 阅读顺序

1. [项目目标](PROJECT_GOAL.md)明确求职定位和 V1 交付条件。
2. [架构](ARCHITECTURE.md)定义被测版本、执行边界、CLI 和产物契约。
3. [实施计划](IMPLEMENTATION_PLAN.md)给出用例数据、阶段任务和验收命令。

## 当前任务 · M4

M3 已验收。沿用现有的离线套件、判定器、目标 checkout 和报告产物。本轮新增真实模型运行，并完成 V1 的求职展示。

- 按实施计划选 DATA-01、DATA-02、DATA-03、DATA-06、CONV-01、SEC-01。通过上游 `OpenAIModelAdapter` 接入一个固定模型，沿用真实 `AgentRuntime.astream` 与只读合成库。每条用例独立跑 3 次，每次重置会话和数据库。
- 单独定义真实模式的判定。业务用例比对实际结果集，仅在问题要求排序时检查顺序。需要固定输出列名时，在真实用例输入中声明列名，预期数值仅供判定器使用。CONV-01 检查首轮结果与追问结果，以及模型收到的真实上一轮历史。SEC-01 检查应用拦截、模型零调用和 SQL 零调用。
- 实际模型输出允许合理的措辞和纠错。记录重试次数、耗时和提供方错误，离线桩的固定措辞和调用次数继续用于离线断言。用 `case_id` 与试验序号标识每次结果。
- 显式传入 `--allow-network` 且本地环境中具备 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 时才发起真实模型请求。凭据缺失时生成 `NOT_RUN` 记录。阻断判定的连接、限流和超时归类为 `ERROR`，业务断言失败归类为 `FAIL`。报告与日志只保存可公开的模型配置和试验信息。
- 更新 README，分别呈现 20 条离线回归、GitHub 红绿检查和真实模型抽样结果。三分钟演示继续用 REL-01 的失败轨迹讲清楚缺失重试与恢复验证。

## M4 验收

```sh
uv run --locked pytest -q
uv run --locked agentreliability run --mode offline --target-dir .targets/commerce --out artifacts/m4/offline
uv run --locked agentreliability run --mode live --allow-network --repeat 3 --target-dir .targets/commerce --out artifacts/m4/live
```

离线套件仍须 20 条全部通过，自动化测试覆盖联网开关、缺凭据和提供方错误。真实报告列出同一模型与目标提交下的 18 次任务，每条用例各有 3 次记录，分别汇总 PASS、FAIL、ERROR 和 NOT_RUN，保留时延、模型调用次数和可用用量。M4 的验收重在有可追溯的实测记录，模型表现按实际结果呈现。若环境缺少凭据，先交付代码、离线验证和待实测命令，再补真实运行证据。

## 工作边界

- 改动范围是本仓库的代码、测试与文档。上游电商 Agent 的固定 checkout 用于读取和运行。
- 离线用例使用合成数据和模型桩。报告保存实际观测证据，区分断言失败、环境错误和未执行。
- 本地开发、只读核验和可复现的测试可以直接完成。远端推送、PR 和仓库设置按用户授权操作。

## 交付回报

完成后列出主要改动、实际执行的命令与结果、报告路径。若遇到环境阻塞，给出具体失败命令、原因和已经验证的部分。
