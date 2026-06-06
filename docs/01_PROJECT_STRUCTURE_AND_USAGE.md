# 01 项目结构与从零开始使用指南

本文档说明当前 ATLAS-Eval 项目的目录结构、主要入口和从零运行方式。ATLAS-Eval 的中文名称是“图元证据多轮对话评估系统”。当前版本默认使用 **LLM** 完成离线建图和可选局部仲裁；本地评估器负责批量评估、证据归因、正负包验收和报告生成。

文档正文保持中文语境。为保证和代码准确对应，少量字段名保留英文，例如 `atom`、`element`、`pool`、`trigger_groups`。它们在中文语境中分别表示评估原子、语义元素、表达池和触发元素组。

## 1. 根目录结构

```text
atlas_eval_project/
├─ app.py
├─ app_graph.py
├─ app_offline.py
├─ README.md
├─ requirements.txt
├─ config/
├─ data/                  # 保留当前数据集，评估时读取 data/dialogues
├─ docs/
├─ example/
├─ prompts/
├─ runs/
├─ scripts/
├─ src/
│  └─ sceg/
├─ tools/
└─ .venv/                 # 只保留占位，不打包真实虚拟环境
```

## 2. 主要运行入口

### 2.1 `app.py`：在线建图 + 评估图形界面

适合展示完整链路：

```text
输入复杂客服指令
→ LLM 五阶段建图
→ 本地结构检查、编译和最终收紧
→ 读取对话包
→ 本地评估器评分
→ 可选 LLM 辅助仲裁
→ 输出中文报告
```

GitHub 清洁版中的演示图统一放在 `example/` 目录：`example/merchant_graph_example.json` 和 `example/rider_graph_example.json`。`runs/` 默认清空，只在运行后生成报告与临时产物。

### 2.2 `app_graph.py`：只建图入口

只生成图表，不评估数据集。适合调试提示词、观察 LLM 输出质量和检查元素结构。它会调用五阶段建图流程：

```text
状态主图
→ 知识表
→ 硬/软限制表
→ 评估原子的一级语义元素
→ 表达池与用户触发话术扩张
```

### 2.3 `app_offline.py`：离线图评估入口

适合稳定展示和复现。它不重新调用 LLM 建图，而是读取已有 `graph.json` 和本地 `data/dialogues`：

```text
选择 graph.json
→ 选择 dialogues 目录
→ 本地编译图表
→ 本地评估样本
→ 可选局部仲裁
→ 生成报告
```

### 2.4 `scripts/run_offline_graph.py`：命令行离线评估入口

适合快速回归、批量验收和打包前自查。

```bash
# 商家示例图
PYTHONPATH=src python scripts/run_offline_graph.py \
  --graph example/merchant_graph_example.json \
  --dialogues data/dialogues \
  --pack all \
  --llm-mode off

# 骑手示例图
PYTHONPATH=src python scripts/run_offline_graph.py \
  --graph example/rider_graph_example.json \
  --dialogues data/dialogues \
  --pack all \
  --llm-mode off
```

## 3. `config/`：运行配置

```text
config/
├─ default_runtime.json
└─ hardcode_guard.json
```

`default_runtime.json` 保存评分权重、阈值、仲裁预算、负包误杀控制和通用中文话语行为算子。它只包含通用机制，不保存商家或骑手的业务答案。

`hardcode_guard.json` 用于约束 `tools/hardcode_guard.py`，防止核心评估代码混入任务硬编码。

## 4. `prompts/`：五阶段建图提示词

```text
prompts/
├─ atlas_eval_method_memory_prompt.md
├─ latest_schema_graph_prompt.md
├─ schema_core_graph_prompt.md
├─ schema_knowledge_table_prompt.md
├─ schema_constraint_tables_prompt.md
├─ schema_atom_element_refinement_prompt.md
├─ schema_element_expansion_prompt.md
└─ schema_graph_repair_prompt.md
```

当前提示词要求 LLM 输出的是可执行评估结构，而不是普通摘要或普通状态机。

| 阶段 | 中文名称 | 输出 | 作用 |
| --- | --- | --- | --- |
| 第一阶段 | 状态主图 | `nodes`、`edges`、`relation_groups`、`terminal_policies` | 建立主流程、条件分支、FAQ 和终止策略。 |
| 第二阶段 | 知识表 | `selector_groups`、`correct_groups`、`wrong_groups`、`value_check` | 建立事实核验对象、正确事实、错误事实和数值校验。 |
| 第三阶段 | 限制表 | `hard_constraint_table`、`soft_constraint_table` | 建立硬限制和软质量限制。 |
| 第四阶段 | 一级语义元素 | 每个评估原子下的 `element_groups` | 从客服期望答话或用户触发种子中拆语义元素。 |
| 第五阶段 | 表达扩张 | 客服侧 `pool`、用户侧 `source_text trigger_groups` | 客服侧扩等价表达池；用户侧扩自然话术并转成触发组。 |

## 5. `src/sceg/`：核心代码目录

当前核心代码位于 `src/sceg/`。主要模块如下：

| 模块 | 中文作用 |
| --- | --- |
| `demo_runner.py` | 总调度器，串联建图、评估、仲裁和报告产物。 |
| `llm_client.py` | LLM 调用、JSON 抽取、缓存、token 统计。 |
| `schema_atomic_pipeline.py` | 表格合并、评估原子登记、元素增量合并、限制清洗。 |
| `schema_supplement_hints.py` | 本地生成二次补图/补表的缺口提示。 |
| `schema_final_tightener.py` | 最终本地收紧：修终止空触发、主线/条件混乱、硬限制重复等。 |
| `schema_linter.py` | 图表结构检查。 |
| `schema_compiler.py` | 把 JSON 图表编译成本地可执行结构。 |
| `element_engine.py` | 语义元素级命中引擎。 |
| `graph_evaluator.py` | 激活子图评分、节点/关系/上下文综合评估。 |
| `knowledge_judge.py` | 知识表的对象召回、正确事实、错误事实和数值判断。 |
| `constraint_judge.py` | 硬/软限制判断。 |
| `dataset_interface.py` | 正负包严格验收。 |
| `oracle_router.py` | 生成本地灰区候选。 |
| `local_second_filter.py` | 仲裁前本地二筛、过滤、合并。 |
| `llm_verifier.py` | LLM 局部仲裁。 |
| `report_explainer.py` / `report_html.py` | 中文报告解释与 HTML 输出。 |

## 6. `tools/`：质量检查与回归工具

```text
tools/
├─ hardcode_guard.py
├─ anti_leak_guard.py
├─ negative_purity_check.py
├─ pre_llm_simulation_audit.py
├─ negative_miss_audit.py
├─ activation_scope_smoke.py
├─ element_bug_regression_smoke.py
├─ role_aware_element_smoke.py
├─ final_tightener_smoke.py
└─ ...
```

推荐交付前运行：

```bash
PYTHONPATH=src python tools/hardcode_guard.py
PYTHONPATH=src python tools/anti_leak_guard.py
PYTHONPATH=src python tools/negative_purity_check.py data/dialogues/negative_pack
PYTHONPATH=src python tools/role_aware_element_smoke.py
PYTHONPATH=src python tools/activation_scope_smoke.py
PYTHONPATH=src python tools/final_tightener_smoke.py
```

这些工具分别检查业务硬编码、负包泄漏、负包标注纯净性、角色感知元素扩张、激活范围和最终图表收紧逻辑。

## 7. `runs/`：图、报告和运行产物

GitHub 清洁包默认清空 `runs/`，只保留占位文件。运行 `app.py`、`app_offline.py` 或命令行评估后，系统才会在 `runs/` 下生成图、报告和统计文件。

常见输出包括：

| 文件 | 说明 |
| --- | --- |
| `graph.json` | 本次使用或生成的图表。 |
| `report.html` | 当前报告入口。 |
| `report_simple.html` | 简版结果报告。 |
| `report_detail.html` | 详细过程报告。 |
| `all_reports_merged.json` | 所有样本结构化评估结果。 |
| `run_token_usage.json` | LLM 调用与 token 使用记录。 |
| `run_timing_summary.json` | 建图、评估、仲裁耗时。 |
| `llm_verifier_summary.json` | 仲裁统计。 |

## 8. 从零开始运行

### 8.1 创建虚拟环境

Windows：

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

### 8.2 配置 LLM

只跑离线图评估且关闭仲裁时，可以不设置 API Key。在线建图或开启辅助仲裁时设置：

```bash
export LLM_API_KEY="你的 LLM API Key"
export LLM_BASE_URL="你的 LLM BASE URL"
export LLM_MODEL="你的LLM NAME"
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY="你的 LLM API Key"
$env:LLM_BASE_URL="你的 LLM BASE URL"
$env:LLM_MODEL="你的LLM NAME"
```

### 8.3 跑离线评估

```bash
python app_offline.py
```

或使用命令行：

```bash
# 商家示例图
PYTHONPATH=src python scripts/run_offline_graph.py \
  --graph example/merchant_graph_example.json \
  --dialogues data/dialogues \
  --pack all \
  --llm-mode off

# 骑手示例图
PYTHONPATH=src python scripts/run_offline_graph.py \
  --graph example/rider_graph_example.json \
  --dialogues data/dialogues \
  --pack all \
  --llm-mode off
```

### 8.4 跑在线建图

```bash
python app.py
```

在界面中粘贴复杂客服指令，填写 LLM API Key，选择建图模式、评估数据包和仲裁模式。系统会把图表、报告、token 统计和耗时统计写入 `runs/` 下对应运行目录。


## 9. 报告查看说明

评估完成后，进入 `runs/` 下对应运行目录，直接用浏览器打开 `report.html` 即可查看报告入口。若需要展示完整证据链，可以打开 `report_detail.html`；若只看通过情况、总分和主要归因，可以打开 `report_simple.html`。GitHub 清洁包初始没有现成报告，报告需要运行后生成。
