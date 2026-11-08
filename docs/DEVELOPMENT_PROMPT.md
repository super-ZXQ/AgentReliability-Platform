# 开发提示词

你是 AgentReliability Platform 的开发代理。请在当前仓库直接完成本轮里程碑，写出可运行的代码并执行验收。

## 阅读顺序

1. [项目目标](PROJECT_GOAL.md)明确求职定位和 V1 交付条件。
2. [架构](ARCHITECTURE.md)定义被测版本、执行边界、CLI 和产物契约。
3. [实施计划](IMPLEMENTATION_PLAN.md)给出用例数据、阶段任务和验收命令。

## 当前任务 · M3

M2 已验收。先检查工作树，把已通过的 M2 保存为本地基线提交。证据位于 `artifacts/m2/`。后续的 GitHub 演示 PR 应以包含 M2 和 `target.lock.json` 的提交为基准。远端同步和仓库设置按用户授权操作。

- 沿用 M2 的 20 条用例、数据、判定器和固定目标版本。完成 `report.html` 与 `compare`。先检查可比性，再按用例 ID 展示新增失败、恢复和环境错误。断言应能定位到对应轨迹事件，报告中的输入和输出按 HTML 转义。
- 在测试适配器中实现 `--demo-fault disable-sql-retry`，让 SQL 执行失败后直接进入结果合成。每份报告记录目标提交和变体。用完整套件证明 REL-01 被拦截，报告列出其他受影响用例。恢复默认行为后，20 条全部通过。
- 实现名为 `agent-regression` 的 GitHub Actions PR 检查。用同一份用例和数据分别运行基准锁和候选锁，确认候选结果覆盖矩阵中的 20 个唯一 ID。保留失败时生成的报告，最终检查状态反映真实门禁结果。缺失基准锁时报告配置错误。
- 更新 README，说明报告查看、红绿复现与合并检查配置。本地验收与远端 CI 分别记录。真实模型实验按 M4 交付。

## M3 本地验收

```sh
uv run --locked pytest -q
uv run --locked agentreliability run --mode offline --target-dir .targets/commerce --out artifacts/m3/healthy
uv run --locked agentreliability run --mode offline --target-dir .targets/commerce --demo-fault disable-sql-retry --out artifacts/m3/injected
uv run --locked agentreliability compare artifacts/m3/healthy/report.json artifacts/m3/injected/report.json --out artifacts/m3/comparison
uv run --locked agentreliability run --mode offline --target-dir .targets/commerce --out artifacts/m3/restored
```

逐条执行并记录退出码。健康与恢复两次运行均应退出 0，完整 20 条通过。注入运行应退出 1，生成可查看的失败报告。对比产物应将 REL-01 列为新增失败，并通过报告定位缺失的重试。另用测试验证用例或数据校验和不一致、缺失必跑用例及环境错误时，门禁均不能通过。

取得授权后，用 GitHub 演示 PR 收集红绿检查链接和可下载产物。若远端验收暂不可执行，交付本地红绿证据，并在验收说明中标明远端状态。

## 工作边界

- 改动范围是本仓库的代码、测试与文档。上游电商 Agent 的固定 checkout 用于读取和运行。
- 离线用例使用合成数据和模型桩。报告保存实际观测证据，区分断言失败、环境错误和未执行。
- 本地开发、只读核验和可复现的测试可以直接完成。远端推送、PR 和仓库设置按用户授权操作。

## 交付回报

完成后列出主要改动、实际执行的命令与结果、报告路径。若遇到环境阻塞，给出具体失败命令、原因和已经验证的部分。
