# SCEG 复杂指令对话评估系统

本项目用于评估客服模型在复杂指令下的多轮应答表现。核心路线是：

```text
复杂指令 → LongCat 离线建图 → 本地结构化证据评估 → 本地二筛 → 少量灰区 LongCat 仲裁 → 中文报告
```

本地代码只执行通用 schema、证据匹配、知识判断、限制判断和报告生成；业务事实必须来自 LongCat 生成的图或任务输入，不能写入 evaluator 逻辑。

## 目录结构

```text
app.py                         # 在线建图 + 评估 GUI 入口
app_offline.py                 # 离线图评估 Demo 入口
config/                        # 运行配置与硬编码审计配置
data/dialogues/                # 正负包对话数据
  positive_pack/merchant/
  positive_pack/rider/
  negative_pack/merchant/
  negative_pack/rider/
docs/                          # 方法说明、清理策略、更新记录
examples/                      # 抽象示例，不用于正式判分
prompts/                       # LongCat 建图 / repair prompt 与任务指令
scripts/run_offline_graph.py   # 离线 graph.json 命令行评估入口
src/sceg2/                     # 核心评估代码
tools/                         # 反硬编码、反泄漏、负包纯度检查工具
runs/                          # 已保留离线图与演示报告例子
  graphs_offline/
  merchant_example/
  ridder_example/
.venv/                         # 空目录占位；真实环境请本地重新创建
```

`data/` 只允许保存正式正负包对话。状态图、报告、缓存、回归结果、LongCat debug 信息都应写入 `runs/`，不要塞进 `data/`。

## 数据规模

| 目录 | 数量 |
| --- | ---: |
| `data/dialogues/positive_pack/merchant` | 91 |
| `data/dialogues/negative_pack/merchant` | 91 |
| `data/dialogues/positive_pack/rider` | 86 |
| `data/dialogues/negative_pack/rider` | 83 |
| 合计 | 351 |

说明：当前包保留经过数据质量清洗后的正负样本，编号保持连续；清理过程未删除对话数据。

## 推荐检查

```bash
PYTHONPATH=src python tools/hardcode_guard.py
PYTHONPATH=src python tools/anti_leak_guard.py
PYTHONPATH=src python tools/negative_purity_check.py data/dialogues/negative_pack
```

本清理版删去了历史回归测试脚本；如需完整开发回归，可以从旧开发包恢复 `tests/`。

## GUI 运行

```bash
python app.py
```

默认 LongCat 模型为 `LongCat-Flash-Lite`。API Key 只在本地输入或通过环境变量传入，不写入代码、配置、报告或压缩包。

## 在线建图与评估

```bash
python app.py
```

该入口适合展示完整链路：输入复杂指令 → LongCat 建图 → 本地评估 → 中文报告生成。清理版不再保留历史命令行回归脚本，避免答辩展示时入口过多。

## 建图模式

GUI 与 `demo_runner` 支持三类建图思路：

- **快速建图**：默认模式。只有节点、知识、限制、ID 对齐等硬缺口才触发二次补图。
- **稳健建图**：质量提醒也会触发二次 repair，适合最终验收。
- **只建一次**：跳过二次补图，适合快速调试界面和本地 evaluator。

二次补图的作用是让 LongCat 根据 audit 补齐结构缺口，而不是让本地代码补业务事实。

## 反硬编码红线

1. 通用代码不能写商家、骑手、样本编号、具体业务答案或具体 domain 的判分特判。
2. `evidence_span` / `wrong_statement` / `injected_errors` 不能编译进 schema 当判分依据。
3. 通用客服表达可以集中在 `src/sceg2/generic_customer_service_expressions.py`，但业务事实不能放进去。
4. `prompts/instructions/` 可以放真实任务指令；通用 prompt 不能写业务例子。
5. LLM 仲裁只处理有 schema 锚点、有对话证据、但本地语义不稳的灰区。

## 文档

- `docs/METHOD_EXECUTION_OVERVIEW.md`：方法与执行链路说明。
- `docs/PACKAGE_CLEANUP_POLICY.md`：交付包清理策略。
- `docs/CHANGELOG.md`：最近版本变更记录。

### 离线图评估 Demo

已有 `graph.json` 时，可以跳过指令输入和建图，直接运行：

```bash
python app_offline.py
```

或使用命令行：

```bash
PYTHONPATH=src python scripts/run_offline_graph.py --graph path/to/graph.json --dialogues data/dialogues --pack all --llm-mode off
```

这个入口只读取离线图并执行本地评估；只有在你主动选择 `shadow` 或 `assist` 且提供 Key 时，才会进行二级判断。
