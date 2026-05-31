# SCEG 复杂指令多轮客服对话评估系统

SCEG（Schema / State-Chart Evidence Grounding）是一个面向复杂客服指令的结构化可解释评估系统。系统先利用 LongCat 将自然语言复杂指令转化为可执行的状态图、知识表与限制表，再由本地 evaluator 对多轮客服对话进行高速、可复现、可解释的批量评估；只有在本地证据不足或语义边界不稳定的灰区，才会进入可选 LongCat 二次仲裁。

```text
复杂客服指令 → LongCat 离线建图 → Schema Linter / Compiler
→ 本地 Graph Evaluator → 本地二筛 → 少量灰区 LongCat 仲裁 → 中文可解释报告
```

当前 README 已拆分为四份文档，建议按顺序阅读：

| 文档 | 作用 |
| --- | --- |
| [`docs/01_PROJECT_STRUCTURE_AND_USAGE.md`](docs/01_PROJECT_STRUCTURE_AND_USAGE.md) | 介绍项目文件夹作用、从零开始运行方式、在线建图与离线图评估 demo。 |
| [`docs/02_METHOD_AND_MODULES.md`](docs/02_METHOD_AND_MODULES.md) | 按评估执行顺序解释系统方法、代码模块、LLM 参与边界和工业可行性。 |
| [`docs/03_EVALUATION_CASE_WALKTHROUGH.md`](docs/03_EVALUATION_CASE_WALKTHROUGH.md) | 以一组商家正包/负包为例，说明一条数据如何走完整个评估流程。 |
| [`docs/04_DATASET_DESIGN.md`](docs/04_DATASET_DESIGN.md) | 介绍数据集构成、多样性来源、商家与骑手正负包覆盖的测试分支。 |

## 当前数据规模

| 数据目录 | 数量 |
| --- | ---: |
| `data/dialogues/positive_pack/merchant` | 91 |
| `data/dialogues/negative_pack/merchant` | 91 |
| `data/dialogues/positive_pack/rider` | 86 |
| `data/dialogues/negative_pack/rider` | 83 |
| 合计 | 351 |

## 最快运行

离线图评估 demo：

```bash
python app_offline.py
```

命令行离线评估：

```bash
PYTHONPATH=src python scripts/run_offline_graph.py \
  --graph runs/graphs_offline/course_publish_upgrade_v1.json \
  --dialogues data/dialogues \
  --pack all \
  --llm-mode off
```

在线建图 + 评估 GUI：

```bash
python app.py
```

默认 LongCat 模型为 `LongCat-Flash-Lite`。项目中的 demo/example 按 `LongCat-Flash-Lite` 作为离线建图与可选二次仲裁模型进行说明；如平台接口侧发生模型名兼容或 fallback，实际调用记录以运行生成的 `run_token_usage.json` 为准。

## 交付红线

本地 evaluator 只保存通用 schema 执行逻辑，不写入商家、骑手或具体业务答案。业务事实必须来自 LongCat 生成的 graph/schema 或任务输入。负包中的 `wrong_statement`、`evidence_span`、`injected_errors` 只用于验收追踪，不能被编译成判分答案。
