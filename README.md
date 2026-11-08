# AgentReliability-Platform

面向工具型 AI Agent 的自动化测试与回归门禁。平台使用固定提交的电商数据分析 Agent 真实工作流，在合成订单库上离线运行用例，输出结构化报告与逐步轨迹。M1 覆盖正常、纠错与结果错误判定；M2 将正式离线套件扩展到 20 条，覆盖数据正确性、多轮会话、执行失败恢复与安全前置拦截。

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

- `report.json`：版本、运行模式、目标仓库与提交、用例与数据 SHA-256、逐条断言及轨迹编号。
- `trace.jsonl`：每行一个事件，记录 `run_id`、`case_id`、试验与轮次、事件编号、来源、步骤、状态和耗时；模型事件附带其实际收到的会话历史。

M2 验收产物位于 `artifacts/m2/`。`.targets/` 与 `artifacts/` 不进入版本控制。
