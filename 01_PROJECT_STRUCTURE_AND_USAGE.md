# 01 项目结构与从零开始使用指南

本文档用于回答两个问题：第一，项目中每个文件夹承担什么作用；第二，评委或新同学拿到项目后，如何从零开始跑通在线建图、离线图评估和报告生成。

## 1. 项目一句话

SCEG 是一个复杂指令多轮客服对话评估系统。它不是把整段对话直接交给大模型打一个黑盒分数，而是先用 LongCat 把复杂客服指令转成可执行的状态图、知识表和限制表，再用本地 evaluator 对对话进行节点履约、知识正确性、限制合规性、上下文转场和样本验收评估。

项目中的 demo/example 按 `LongCat-Flash-Lite` 作为离线建图与可选二次仲裁模型进行说明。也就是说，LongCat 负责把复杂指令结构化为 graph/schema，并在本地无法稳定判断的少量灰区中做二级判断；大规模样本评估本身由本地代码完成。

## 2. 根目录文件说明

```text
sceg_longcat_project/
├─ app.py
├─ app_offline.py
├─ README.md
├─ requirements.txt
├─ config/
├─ data/
├─ docs/
├─ examples/
├─ prompts/
├─ runs/
├─ scripts/
├─ src/
├─ tools/
└─ .venv/
```

### `app.py`：在线建图 + 评估 GUI

`app.py` 是完整链路的图形界面入口。它适合答辩时展示系统如何从一段复杂客服指令开始，自动生成结构化评估标准，再评估本地对话包并输出中文报告。

运行后，界面会依次完成：

```text
输入复杂指令
→ 调用 LongCat-Flash-Lite 离线建图
→ Schema Linter / Compiler 结构检查与编译
→ 本地 evaluator 评估正负包
→ 可选 LongCat 二级判断
→ 生成 HTML / JSON 报告
```

界面中的“建图模式”有三种：

| 模式 | 作用 |
| --- | --- |
| 快速建图 | 默认模式。只有节点、知识、限制、ID 对齐等硬缺口才触发二次补图。 |
| 稳健建图 | 质量提醒也会触发二次 repair，适合最终验收。 |
| 只建一次 | 跳过二次补图，适合快速调试界面或检查本地 evaluator。 |

### `app_offline.py`：离线图评估 Demo

`app_offline.py` 是稳定演示入口。它不重新输入复杂指令，也不重新调用 LongCat 建图，而是直接读取已有的离线 `graph.json` 和本地 `data/dialogues` 对话包进行评估。

它适合比赛展示，因为结果更可复现：

```text
选择离线 graph.json
→ 选择 dialogues 目录
→ 本地编译 graph
→ 本地评估对话
→ 生成中文报告
```

默认情况下，离线 demo 的“二级判断”为关闭，不会调用大模型。只有用户主动选择“审计模式”或“辅助模式”，并填写 API Key 时，才会把本地二筛后的少量灰区发送给 LongCat-Flash-Lite 做局部仲裁。

### `requirements.txt`：环境依赖

项目主要依赖 Python 标准库和少量界面/报告所需包。建议使用独立虚拟环境安装，避免和系统 Python 或其他项目冲突。

### `.venv/`：虚拟环境占位

交付包中 `.venv/` 只保留 `.gitkeep` 占位，不包含真实虚拟环境。拿到项目后需要在本机重新创建虚拟环境。

## 3. `config/`：运行配置

```text
config/
├─ default_runtime.json
└─ hardcode_guard.json
```

`default_runtime.json` 保存 evaluator 的运行参数，包括：

- 四维评分权重：节点完成度、结构关系、知识正确性、限制合规性；
- 正包通过阈值、节点命中阈值、负包分数 cap；
- LongCat 仲裁候选预算；
- 通用中文话术算子，例如承诺、施压、规则说明等。

这些配置只包含通用评价参数和通用语言算子，不写入商家、骑手或具体任务答案。

`hardcode_guard.json` 是反硬编码检查配置，用于辅助 `tools/hardcode_guard.py` 检查核心代码里是否混入了不该出现的业务词。

## 4. `data/`：正式正负包对话数据

```text
data/dialogues/
├─ positive_pack/
│  ├─ merchant/
│  └─ rider/
└─ negative_pack/
   ├─ merchant/
   └─ rider/
```

`data/` 只保存正式对话数据，不保存运行产物。当前规模为：

| 数据包 | 数量 |
| --- | ---: |
| 商家正包 `positive_pack/merchant` | 91 |
| 商家负包 `negative_pack/merchant` | 91 |
| 骑手正包 `positive_pack/rider` | 86 |
| 骑手负包 `negative_pack/rider` | 83 |
| 合计 | 351 |

正包用于检验系统能否认可合格客服对话；负包用于检验系统能否识别流程缺失、知识错误、限制违规等问题。

## 5. `docs/`：项目说明文档

`docs/` 保存评委阅读材料、方法说明、数据集说明和清理策略。当前 README 被拆成四份主要文档：

```text
docs/01_PROJECT_STRUCTURE_AND_USAGE.md
docs/02_METHOD_AND_MODULES.md
docs/03_EVALUATION_CASE_WALKTHROUGH.md
docs/04_DATASET_DESIGN.md
```

其他辅助文档包括：

- `METHOD_EXECUTION_OVERVIEW.md`：开发阶段的方法链路梳理；
- `PACKAGE_CLEANUP_POLICY.md`：交付包清理原则；
- `CHANGELOG.md`：近期版本变更记录。

## 6. `examples/`：抽象最小示例

```text
examples/
├─ dialogue_positive.json
├─ dialogue_negative.json
└─ graph_abstract.json
```

`examples/` 里的文件是抽象示例，用于理解格式，不作为正式评测数据。正式评估使用 `data/dialogues/` 中的商家、骑手正负包；正式离线图使用 `runs/graphs_offline/` 中的 graph。

## 7. `prompts/`：LongCat 建图提示词和任务指令

```text
prompts/
├─ latest_schema_graph_prompt.md
├─ schema_graph_repair_prompt.md
└─ instructions/
   ├─ merchant_instruction.txt
   └─ rider_instruction.txt
```

`latest_schema_graph_prompt.md` 用于第一次 LongCat 建图，要求输出状态主图、知识表、限制表、终止策略等结构化 schema。

`schema_graph_repair_prompt.md` 用于二次补图。当本地 schema gap audit 发现结构缺口时，系统会把原始复杂指令、当前 schema、审计结果和高层绑定提示发送给 LongCat-Flash-Lite，让它返回一份完整修正后的 schema。

`prompts/instructions/` 保存商家和骑手任务指令，便于复现实验和展示。注意：prompt 可以包含任务指令，但本地 evaluator 代码不能写死任务答案。

## 8. `runs/`：离线图、演示报告和运行产物

```text
runs/
├─ graphs_offline/
│  ├─ course_publish_upgrade_v1.json
│  └─ flyleg_rider_call_v1.json
├─ merchant_example/
├─ ridder_example/
├─ latest_run.json
└─ latest_offline_run.json
```

`runs/graphs_offline/` 保存已经生成好的离线状态图。当前有两份：

| 文件 | 对应任务 |
| --- | --- |
| `course_publish_upgrade_v1.json` | 商家课程发布页直播升级通知。 |
| `flyleg_rider_call_v1.json` | 飞毛腿骑手合同通知与配送提醒。 |

`runs/merchant_example/` 和 `runs/ridder_example/` 保存演示报告例子，包括：

- `report.html`：当前选择的报告入口；
- `report_simple.html`：简版结果报告；
- `report_detail.html`：详细过程报告；
- `all_reports_merged.json`：所有样本的结构化评估结果；
- `llm_verifier_summary.json`：二级判断统计；
- `run_token_usage.json`：LongCat token 使用记录；
- `run_timing_summary.json`：运行耗时统计；
- `upload_bundle.zip`：一次运行结果的压缩包。

这些 example 体现的是：LongCat-Flash-Lite 负责离线建图和可选二次仲裁，本地 evaluator 负责批量评估和生成中文可解释报告。

## 9. `scripts/`：命令行入口

```text
scripts/run_offline_graph.py
```

当前清理版只保留一个命令行入口，用于离线 graph 评估。这样可以减少展示时的入口混乱。

基本命令：

```bash
PYTHONPATH=src python scripts/run_offline_graph.py \
  --graph runs/graphs_offline/course_publish_upgrade_v1.json \
  --dialogues data/dialogues \
  --pack all \
  --llm-mode off
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--graph` | 离线状态图路径。 |
| `--dialogues` | 对话数据根目录。 |
| `--pack` | `all`、`positive` 或 `negative`。 |
| `--max` | 只评估前 N 条，便于快速演示。 |
| `--llm-mode` | `off`、`shadow` 或 `assist`。 |

## 10. `src/sceg2/`：核心评估代码

`src/sceg2/` 是项目的核心。它包含：

- LongCat 客户端；
- schema 编译和检查；
- 对话加载和证据抽取；
- 节点匹配、知识判断、限制判断、上下文转场；
- 正负包验收；
- 本地二筛和可选 LongCat 仲裁；
- 报告解释和 HTML 生成。

核心原则是：本地代码只实现通用结构化评估逻辑，业务事实都来自 graph/schema。

## 11. `tools/`：质量检查工具

```text
tools/
├─ hardcode_guard.py
├─ anti_leak_guard.py
└─ negative_purity_check.py
```

三个工具分别用于：

| 工具 | 作用 |
| --- | --- |
| `hardcode_guard.py` | 检查核心代码是否混入业务硬编码。 |
| `anti_leak_guard.py` | 检查 `wrong_statement`、`evidence_span` 等负包答案字段是否被编译进判分逻辑。 |
| `negative_purity_check.py` | 检查负包是否保留明确的预设错误和结构化验收字段。 |

推荐每次交付前运行：

```bash
PYTHONPATH=src python tools/hardcode_guard.py
PYTHONPATH=src python tools/anti_leak_guard.py
PYTHONPATH=src python tools/negative_purity_check.py data/dialogues/negative_pack
```

## 12. 从零开始运行项目

### 12.1 创建虚拟环境

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux / macOS：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 12.2 设置 LongCat 参数

如果只跑离线图评估且关闭二级判断，可以不设置 API Key。

如果要在线建图或开启二级判断，需要设置：

Windows PowerShell：

```powershell
$env:LONGCAT_API_KEY="你的 LongCat API Key"
$env:LONGCAT_BASE_URL="https://api.longcat.chat/openai"
$env:LONGCAT_MODEL="LongCat-Flash-Lite"
```

Linux / macOS：

```bash
export LONGCAT_API_KEY="你的 LongCat API Key"
export LONGCAT_BASE_URL="https://api.longcat.chat/openai"
export LONGCAT_MODEL="LongCat-Flash-Lite"
```

API Key 只应放在本地环境变量或界面输入框中，不要写进代码、配置文件、报告或压缩包。

### 12.3 跑离线图评估 demo

图形界面：

```bash
python app_offline.py
```

命令行快速验证商家包：

```bash
PYTHONPATH=src python scripts/run_offline_graph.py \
  --graph runs/graphs_offline/course_publish_upgrade_v1.json \
  --dialogues data/dialogues \
  --pack all \
  --llm-mode off
```

命令行快速验证骑手包：

```bash
PYTHONPATH=src python scripts/run_offline_graph.py \
  --graph runs/graphs_offline/flyleg_rider_call_v1.json \
  --dialogues data/dialogues \
  --pack all \
  --llm-mode off
```

### 12.4 跑在线建图 + 评估

```bash
python app.py
```

在界面中粘贴复杂客服指令，填写 LongCat API Key，选择建图模式、评估数据包和二级判断模式。系统会把运行产物写入 `runs/longcat_latest__时间戳/`。

### 12.5 查看输出

一次运行结束后，重点查看：

| 文件 | 说明 |
| --- | --- |
| `report.html` | 当前选择的报告入口。 |
| `report_simple.html` | 面向评委的简版结果报告。 |
| `report_detail.html` | 面向技术评审的详细过程报告。 |
| `all_reports_merged.json` | 所有样本的结构化评估明细。 |
| `graph.json` | 本次使用的状态图。 |
| `run_manifest.json` | 本次运行索引。 |
| `run_token_usage.json` | LongCat 调用和 token 统计。 |
| `run_timing_summary.json` | LongCat 建图、本地评估等耗时统计。 |

## 13. 推荐展示路径

答辩展示可以按下面顺序：

1. 打开 `app_offline.py`，选择 `runs/graphs_offline/course_publish_upgrade_v1.json`；
2. 选择 `data/dialogues`，评估范围选择“全部数据”或“只跑前几条”；
3. 二级判断先选择“关闭”，展示本地 evaluator 的速度和可解释性；
4. 生成报告后打开 `report_simple.html`，给评委看通过率、分数和失分归因；
5. 再打开 `report_detail.html`，展示节点命中、知识判断、限制判断、样本验收追踪；
6. 最后说明：LongCat-Flash-Lite 的高成本语义理解只发生在离线建图和少量灰区仲裁阶段，大规模评估由本地结构化 evaluator 完成。
