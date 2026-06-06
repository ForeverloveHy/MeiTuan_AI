# 02 方法与代码模块说明：当前版本的完整执行细节

本文档按当前代码的真实执行顺序说明 ATLAS-Eval 方法。它不是概念性介绍，而是尽量把每个关键字段、每个模块的输入输出、语义元素层的命中逻辑、节点如何算分、知识和限制如何判定、正负包如何验收讲清楚。

为保持中文语境，本文统一使用以下中文术语。括号中是代码或图表中的字段名。

| 中文术语 | 代码字段 / 模块名 | 含义 |
|---|---|---|
| 状态主图 | `nodes / edges / relation_groups / terminal_policies` | 复杂客服指令中的流程、分支、转场和终止策略。 |
| 知识表 | `knowledge_table` | 事实核验表，判断客服是否把业务事实说错。 |
| 硬限制表 | `hard_constraint_table` | 有明确负向对象或禁止动作的硬合规边界。 |
| 软限制表 | `soft_constraint_table` | 没有明确负向对象的整体话术质量要求。 |
| 图表评估原子 | `atom` / `atom_id` | 从状态主图、知识表、限制表中登记出来的最小可评估对象，包括触发、节点小任务、知识项、硬限制、软限制。 |
| 对话原子 | `DialogueAtom` | 从真实对话证据单元中切出来的局部文本片段，是本地匹配时的候选证据载体。 |
| 评估原子 | `atom` 的统称 | 文档中不加限定时，通常指图表评估原子；当讨论运行时证据时会明确写成对话原子。 |
| 语义元素 | `element` | 从客服预期答话或用户触发话术中拆出的可匹配短语。 |
| 主元素 | `main: true` | 用来召回候选证据的核心短语。 |
| 事实元素 | `fact: true` | 用来做精判的数字、时间、金额、方向、关系、结果等事实槽。 |
| 表达池 | `pool` | 严格等价表达池，只扩同义或口语变体，不新增事实。 |
| 触发元素组 | `trigger_groups` | 用户侧触发节点的 OR 组。每组通常来自一条用户可能说法 `source_text`。 |
| 元素组 | `element_groups` | 一个评估原子下的 AND/OR 式局部语义单元。 |
| 激活子图 | active subgraph | 被用户触发、主线推进或终止策略实际覆盖的图中局部路径。 |
| 本地评估器 | `GraphEvaluator` | 本地执行节点、关系、知识、限制和分数计算的主模块。 |

当前版本最核心的变化是：**系统不再把 LLM 输出当作一组简单关键词规则，也不再用旧式 requirement/evidence group 作为主评分对象，而是建立了“双 atom”机制：先把图表对象登记为图表评估原子，再把真实对话切成对话原子；二者通过 element / pool / candidate window 在本地完成候选召回、精判、评分和审计。**

---

## 1. 当前完整执行链路

一次完整运行可以分成 18 个环节：

```text
复杂客服指令 / 离线 graph
→ LLM 五阶段建图
→ 本地缺口提示与二次补图/补表
→ 图表合并、清洗、编译和最终收紧
→ 对话读取与业务域过滤
→ 证据单元抽取
→ 对话评估原子构造
→ 语义元素命中
→ 节点激活与激活子图评分
→ 关系和终止策略评分
→ 知识表核验
→ 硬限制 / 软限制核验
→ 四维总分和上限机制
→ 正负包严格验收
→ 灰区候选生成
→ 本地二筛
→ 可选 LLM 局部仲裁
→ 中文报告与运行产物输出
```

对应的主要代码模块如下：

| 执行环节 | 主要代码文件 | 说明 |
|---|---|---|
| LLM 调用 | `llm_client.py` | API 调用、JSON 提取、token 统计、错误恢复。 |
| 建图编排 | `demo_runner.py` | 五阶段建图、补图、评估、报告产物汇总。 |
| 图表原子化 | `schema_atomic_pipeline.py` | 合并知识/限制表、生成评估原子、合并元素扩张结果。 |
| 二次补图提示 | `schema_supplement_hints.py` | 生成本地通用缺口提示，不写业务答案。 |
| 硬限制兜底 | `hard_constraint_backfill.py` | 从原复杂指令中的明确负向语言抽 hard skeleton。 |
| 图结构编译 | `schema_compiler.py` | 把 JSON 图编译为本地 `StateGraph` 对象。 |
| 最终收紧 | `schema_final_tightener.py` | 修复主线/条件线、terminal 空触发、hard 去重等。 |
| 对话读取 | `dialogue_loader.py` | 读取正负包 JSON，统一 turns 格式。 |
| 证据抽取 | `evidence_extractor.py` | 把多轮话术转为 `EvidenceUnit`。 |
| 语义元素执行 | `element_engine.py` | 构造 `DialogueAtom`，执行元素召回、局部窗口、加权评分。 |
| 主图评估 | `graph_evaluator.py` | 节点激活、节点小任务、关系、上下文转场和总分。 |
| 知识核验 | `knowledge_judge.py` | selector/correct/wrong/value_check/方向冲突。 |
| 限制核验 | `constraint_judge.py` | hard 负向对象与 safe side；soft 统计指标。 |
| 正负包验收 | `dataset_interface.py` | 正包严格通过、负包预设错误命中与误杀控制。 |
| 分数同步 | `score_adjuster.py` | 让严重错误的样本最终分数和验收结论一致。 |
| 灰区候选 | `oracle_router.py` | 生成局部仲裁候选。 |
| 本地二筛 | `local_second_filter.py` | 过滤、合并、局部提升候选。 |
| LLM 仲裁 | `llm_verifier.py` | 只判断局部候选，不整段重评。 |
| 报告 | `report_explainer.py / report_html.py` | 生成中文归因和 HTML。 |

---

## 2. 输入层：复杂指令、图表、对话包和运行配置

### 2.1 复杂客服指令

复杂客服指令是唯一的业务事实来源。LLM 可以从指令里抽流程、事实和限制；本地代码只能执行通用结构化评估逻辑，不能把商家/骑手业务答案写死在代码里。

### 2.2 对话包

对话样本来自 `data/dialogues/`，分为正包和负包。字段通常包括：

| 字段 | 含义 |
|---|---|
| `id` / `dialogue_id` | 样本 ID。 |
| `sample_type` | `positive` 或 `negative`。 |
| `domain` | 业务域，例如 merchant / rider。 |
| `turns` | 多轮对话，每轮包含 speaker 和 text。 |
| `coverage_targets` | 正包的场景覆盖目标，可用于场景型正包验收。 |
| `injected_errors` | 负包预设错误，仅用于验收对齐和报告解释，不能直接参与评分。 |

负包中的 `wrong_statement`、`evidence_span` 不能编译成判分规则。代码中 `anti_leak_guard.py` 会检查这条红线。

### 2.3 运行配置 `config/default_runtime.json`

当前核心配置包括：

```json
{
  "model": "读取环境变量 LLM_MODEL",
  "weights": {
    "node_completion": 0.5,
    "relation_score": 0.1,
    "knowledge_score": 0.2,
    "constraint_score": 0.15,
    "soft_constraint_score": 0.05
  },
  "thresholds": {
    "positive_pass": 90.0,
    "node_satisfied": 0.75,
    "node_partial": 0.35,
    "positive_component_mins": {
      "node_completion": 78.0,
      "relation_score": 70.0,
      "knowledge_score": 98.0,
      "constraint_score": 98.0
    },
    "negative_max_unexpected_bad_events": 0
  }
}
```

这些配置体现了现在的验收原则：正包不仅要总分高，还要各组件分过线；负包不仅要识别预设错误，还要尽量没有无关误杀。

---

## 3. LLM 五阶段建图：从复杂指令到可执行图表

当前建图不再是一条 prompt 一次性生成所有内容，而是多阶段契约式生成。

### 3.1 第一阶段：状态主图 `schema_core_graph_prompt.md`

输出对象：

```text
nodes
edges
relation_groups
terminal_policies
```

状态主图只负责“客服应该怎么推进流程”。它要区分：

| 节点类型 | 作用 |
|---|---|
| `start` | 开场、身份确认。 |
| `main` | 主线必达流程。 |
| `branch` | 条件分支，例如不是负责人、不知情、不想配送。 |
| `faq` | 用户追问后才回答的问题。 |
| `out_of_scope` | 超职责问题。 |
| `terminal` | 开车、坚持无法继续等终止状态。 |

核心原则：

1. 主线节点不能混入未触发 FAQ。
2. 条件分支不能进入 required sequential 主线。
3. terminal 节点必须有明确用户触发，不允许 optional 且无触发组。
4. 信息获取类节点不能写成“必须主动问一遍”，应写成“确认或获取该信息；用户已提供则直接使用”。

### 3.2 第二阶段：知识表 `schema_knowledge_table_prompt.md`

知识表不是流程节点，也不是客服必须主动说完的清单。它只在客服触及某个事实对象时判断有没有说错。

当前知识表字段：

| 字段 | 含义 | 写法要求 |
|---|---|---|
| `knowledge_id` / `id` | 知识项 ID。 | 可用父知识 ID + atom ID。 |
| `name` | 中文名称。 | 用于报告展示。 |
| `severity` | 严重程度。 | high / medium / low。 |
| `text` | 知识事实说明。 | 人可读，不作为唯一匹配规则。 |
| `selector_groups` | 召回对象。 | 只能写对象和属性，不能写正确答案。 |
| `correct_groups` | 正确事实。 | 写正确值、正确方向、正确关系。 |
| `wrong_groups` | 典型错误事实。 | 写常见反向事实或错误关系。 |
| `value_check` | 数字/时间/金额/单量/时长等严格值校验。 | 必须有 `checks / expected_value / unit / slot_anchors`。 |
| `negation_rule` | 否定与反转处理。 | 处理“不会/不影响/未显示”等否定作用域。 |

最重要的契约是：

```text
selector_groups 只负责“召回这句话在谈哪个对象”；
correct_groups / wrong_groups / value_check 才负责事实对错。
```

如果 selector 写了正确答案，例如“低延迟直播延迟 1-2 秒”，负包说成“低延迟直播延迟 5-10 秒”时反而可能召回失败，所以提示词中明确禁止 selector 包含正确值。

### 3.3 第三阶段：硬限制表和软限制表 `schema_constraint_tables_prompt.md`

限制表分成 hard 和 soft。

#### 硬限制表

硬限制有明确负向对象或明确禁止动作，例如：

```text
不能承诺优惠券 / 折扣券
开车时不得继续推进流程
不得承诺站长可干预排名
不得代用户操作退出
禁用“哈哈、嘿嘿、嘻嘻”等明确词表
```

典型字段：

| 字段 | 含义 |
|---|---|
| `constraint_id` / `id` | 限制项 ID。 |
| `name` | 中文名称。 |
| `enforcement` | `hard`。 |
| `constraint_kind` | negative_object_boundary / semantic_object / structural_metric 等。 |
| `trigger_groups` | 该限制依赖用户场景时的触发组。 |
| `negative_groups` | 违规侧：受限对象 + 禁止动作。 |
| `safe_groups` | 安全侧：同一对象下的安全回应。 |
| `severity` | 严重程度。 |

硬限制判断不是简单看到“保证”就判违规，而是要看：

```text
受限对象是否出现 + 违规动作是否出现 + 同一候选内安全侧是否没有命中。
```

#### 软限制表

软限制主要处理整体话术质量，例如简洁、自然、不重复、不过长。它通常没有明确负向对象，不应和 hard 混在一起。

如果出现“不说/不能说/禁用 + 明确词表”，即使是语气词，也必须进入 hard，而不是 soft。

### 3.4 第四阶段：评估原子一级语义元素 `schema_atom_element_refinement_prompt.md`

第四阶段先把图表中所有可执行对象统一登记成评估原子：

| atom 来源 | 例子 | 用途 |
|---|---|---|
| `activation` | 用户说在开车、用户问退出、用户表示不知情。 | 判断节点是否被触发。 |
| `node_atom` | 客服应说明低延迟区别、询问是否可以配送。 | 判断流程履约。 |
| `knowledge` | 标准直播延迟约 5-10 秒。 | 判断事实正确性。 |
| `hard_constraint` | 禁止承诺优惠券。 | 判断合规违规。 |
| `soft_constraint` | 话术简洁不重复。 | 判断软质量。 |

每个原子都有稳定 `atom_id`，后续一级元素、二级表达池和报告归因都靠这个 ID 对齐。

第四阶段的关键是角色感知：

```text
客服侧 node_atom / knowledge / hard / soft：
先想象客服最可能说出的自然答话，再从这句话拆 element。

用户侧 activation / condition / user_triggered：
第四步只给最小触发种子，不大量扩写用户话术。
```

这样做是因为中文客服电话中，客服话术往往由系统和指令收敛，表达比较稳定；用户话术开放度高，必须在第五步先扩用户 source_text。

### 3.5 第五阶段：二级表达扩张 `schema_element_expansion_prompt.md`

第五阶段分两条路径。

#### 客服侧：扩元素表达池

对已有语义元素扩 `pool`：

```json
{"value": "稍后再打", "main": true, "fact": false, "pool": ["稍后联系", "过会儿再打", "回头再联系"]}
```

要求：

1. 保留原 `value/main/fact`。
2. 只能扩严格等价表达。
3. 不能新增事实、改数值、改方向。
4. 对 `fact=true` 的数字、时间、金额只能做格式等价，例如“18点 / 18:00 / 下午6点”。

#### 用户侧：扩 source_text，再 element 化

用户 trigger 不能只写“忙、问题、不知情”这类抽象元素。第五步要生成多条用户可能话术：

```json
{
  "source_text": "我现在正在开车，不方便听",
  "elements": [
    {"value": "开车", "main": true, "fact": false, "pool": ["正在开车", "路上开车"]},
    {"value": "不方便", "main": true, "fact": false, "pool": ["不方便接", "不方便听"]}
  ]
}
```

每个 `source_text` 变成一组 `trigger_group`。多组之间是 OR，任意一组命中即可触发节点。

---

## 4. 图表字段详解

### 4.1 顶层字段

| 字段 | 含义 |
|---|---|
| `graph_id` | 图 ID，例如商家直播升级或飞毛腿骑手通知。 |
| `name` | 图中文名称。 |
| `metadata` | 模型、阶段、质量门、token、耗时等元信息。 |
| `nodes` | 状态主图节点。 |
| `edges` | 节点之间的转场边。 |
| `relation_groups` | 多节点结构关系，例如主线顺序、互斥分支、任一 FAQ。 |
| `terminal_policies` | 触发终止或压制后续流程的策略。 |
| `knowledge_table` | 知识核验表。 |
| `hard_constraint_table` | 硬限制表。 |
| `soft_constraint_table` | 软限制表。 |

### 4.2 节点字段

```json
{
  "node_id": "n05",
  "name": "传达升级内容",
  "node_type": "main",
  "required": true,
  "activation": {...},
  "atoms": [...],
  "aliases": [...]
}
```

| 字段 | 含义 | 执行影响 |
|---|---|---|
| `node_id` / `id` | 节点稳定 ID。 | 关系边、报告、负包标签对齐使用。 |
| `name` | 中文名称。 | 主要用于报告展示。 |
| `node_type` / `type` | 节点类型。 | 决定是否主线、分支、FAQ、终止。 |
| `required` | 是否核心节点。 | 主线必达节点缺失会影响正包通过。 |
| `activation` | 激活条件。 | 决定节点是否进入激活子图。 |
| `atoms` | 节点内部小任务。 | 节点分由 atom 命中结果汇总。 |
| `aliases` | 别名。 | 负包标签和报告绑定辅助。 |

### 4.3 激活字段 `activation`

```json
{
  "mode": "condition",
  "trigger_hint": "用户表示正在开车",
  "trigger_groups": [...],
  "match_policy": {...}
}
```

| 字段 | 含义 |
|---|---|
| `mode` | `always / optional / user_triggered / condition`。 |
| `trigger_hint` | 人可读触发说明，不应作为唯一执行依据。 |
| `trigger_groups` | 用户触发元素组，是真正执行的主要触发条件。 |
| `source_text` | 某一组触发元素对应的模拟用户原话。 |
| `match_policy` | 局部匹配策略，例如是否允许 assistant 自激活。 |

执行规则：

1. `always` 的主线节点默认激活。
2. `condition/user_triggered` 节点必须由用户触发组命中，或在特定拓扑条件下被主线提升。
3. `faq/out_of_scope` 可以在客服确实回答了该节点时自激活，但普通 branch 不能靠客服泛泛安全话术自激活。
4. `terminal` 必须由用户或上下文触发，不能靠客服一句“稍后再联系”自己触发终止。

### 4.4 节点原子 `atoms`

```json
{
  "atom_id": "a07",
  "name": "说明低延迟直播特点",
  "text": "低延迟直播延迟约1-2秒，互动更流畅，适合小班课或实操课",
  "required": true,
  "severity": "medium",
  "element_groups": [...]
}
```

| 字段 | 含义 |
|---|---|
| `atom_id` / `id` | 节点内部小任务 ID。 |
| `name` | 小任务中文名。 |
| `text` | 小任务说明。提示词用它生成客服预期答话，但运行时主要看 `element_groups`。 |
| `required` | 是否必需。必需 atom 未命中会压低节点分。 |
| `severity` | high/medium/low。可影响报告和权重。 |
| `element_groups` | 执行命中的语义元素组。 |

### 4.5 语义元素组 `element_groups`

```json
{
  "group_id": "g1",
  "role": "main",
  "require_all_main": false,
  "elements": [...]
}
```

| 字段 | 含义 |
|---|---|
| `group_id` | 组 ID。没有时本地按顺序补。 |
| `role` / `group_role` | main / selector / positive / negative / trigger / safe / global。 |
| `require_all_main` | 是否要求该组所有 main 主干都命中。 |
| `elements` | 元素列表。 |

同一个 atom 可以有多个 element group。对节点来说，多个 main group 通常是替代表达；对知识和限制来说，不同 group 可表示不同正确/错误侧。

### 4.6 语义元素 `elements`

```json
{"value": "低延迟直播", "main": true, "fact": false, "pool": ["低延时直播", "低延迟模式直播"]}
```

| 字段 | 含义 | 命中作用 |
|---|---|---|
| `value` | 元素核心短语。 | 参与文本匹配。 |
| `main` | 是否主元素。 | 只有 main 和它的 pool 能召回候选 atom。 |
| `fact` | 是否事实开关。 | 知识/限制中 fact 缺失时直接 miss。 |
| `pool` | 等价表达池。 | 让自然中文口语可以命中同一个元素。 |

关键规则：

1. `main=true` 不代表必须每个主元素都字面出现，而是参与主干召回和加权评分。
2. `fact=true` 是更严格的精判开关；在知识和限制侧，如果 fact 没命中，本地不会把候选当成支持/冲突/违规。
3. `pool` 不能扩成知识库，只能扩表面等价表达。
4. “我、你、用户、客户、骑手、商家、问题、情况”等泛词不能作为用户 trigger 的唯一主元素。

---

## 5. 图表合并、清洗、编译和最终收紧

### 5.1 `schema_atomic_pipeline.py`

这个文件负责把 LLM 五阶段输出合并成一张可执行图。

主要函数：

| 函数 | 作用 |
|---|---|
| `merge_knowledge_table()` | 合并第二阶段知识表，支持父知识项下的 atoms 展平。 |
| `merge_constraint_tables()` | 合并 hard/soft 限制表。 |
| `sanitize_constraint_tables()` | 清洗 hard/soft，移除软硬混淆、空 negative hard、重复 hard。 |
| `merge_constraint_supplement()` | 合并二次补表 patch。 |
| `assign_element_anchor_ids()` | 给 activation、node atom、knowledge、hard、soft 分配稳定 element anchor。 |
| `build_atom_transport()` | 生成进入第四步的评估原子传输层。 |
| `build_atom_registry()` | 汇总所有评估原子。 |
| `normalize_executable_groups()` | 规范化 selector/correct/wrong/negative/safe/element_groups。 |
| `merge_element_anchor_delta()` | 合并第四步一级元素或第五步二级表达池。 |

### 5.2 hard 表治理

`sanitize_constraint_tables()` 会做几件事：

1. 明显软质量规则不能留在 hard。
2. hard 必须有可执行 `negative_groups` 或可转换的负向对象。
3. 同类 hard 归并，例如禁用表达、承诺权益、安全停止、职责范围、代操作。
4. hard 过泛时优先保留具体对象。
5. 限制 hard 数量，防止 hard 表爆炸。

### 5.3 本地 hard 兜底 `hard_constraint_backfill.py`

如果原复杂指令里有明确硬边界，但 LLM 没抽出来，本地会抽候选表：

```json
{
  "source_quote": "原指令证据",
  "boundary_kind": "no_unfounded_promise / safety_stop_boundary / out_of_scope_boundary / forbidden_phrase",
  "restricted_object": "受限对象",
  "forbidden_action": "违规动作",
  "safe_action": "安全动作",
  "must_be_hard": true
}
```

然后转换为正式 hard constraint。它只依据通用负向句式，不写业务硬编码。

### 5.4 最终收紧 `schema_final_tightener.py`

最终收紧层用于修复 LLM 常见结构病灶，包括：

1. required sequential 中的 branch/faq/terminal 移出强主线。
2. 核心说明节点误写成 user_triggered 时改回主线。
3. terminal 节点缺用户触发时补最小 trigger_groups。
4. 忙碌但继续说明的节点不能 suppress 主线，要回流主线。
5. 信息获取 atom 改成“确认或根据用户已提供信息获取”。
6. hard 去重和补缺。

这层是结构兜底，不直接编造业务事实。

---

## 6. 对话读取和证据抽取

### 6.1 `dialogue_loader.py`

统一读取 JSON 格式，把新旧字段转成统一对话对象。它只做加载，不评分。

### 6.2 `evidence_extractor.py` 与 `EvidenceUnit`

每轮对话会变成证据单元：

| 字段 | 含义 |
|---|---|
| `turn_index` | 轮次。 |
| `speaker` | user / assistant。 |
| `text` | 原始文本。 |
| `normalized_text` | 规范化文本。 |
| `numbers` | 显式数字、范围和单位。 |

证据抽取不判断业务对错，只保留后续匹配需要的文本和数字信息。

---

## 7. 语义元素层：图表 atom 与 DialogueAtom 如何对齐命中

这是当前版本最重要的执行层，对应 `element_engine.py`。它不是只处理图表里的 atom，也不是把原始对话直接拿去和整句模板比较，而是建立两类对象：

```text
图表评估原子 schema atom：来自状态主图、知识表、限制表，表示“应该评什么”。
对话原子 DialogueAtom：来自真实对话切片，表示“对话里可能有什么证据”。
```

二者的关系可以概括为：

```text
图表 atom 先被拆成 element_groups
真实 dialogue 先被切成 DialogueAtom
DialogueAtom 再补充本地通用 elements
图表 atom 的 main / pool 去召回 DialogueAtom 候选
候选窗口内再展开 value / aliases / pool 做元素命中
最终得到 hit / review / miss 和 element_audit
```

### 7.1 图表 atom：评估目标的元素化

图表 atom 来自离线 schema，包括 `activation`、`node_atom`、`knowledge`、`hard_constraint`、`soft_constraint`。它的 element 由第四、第五阶段产生：

1. 第四阶段把 atom_text 拆成短语级 element。
2. 第五阶段为 element 扩展严格等价的 `pool`。
3. 客服侧 atom 通常从 expected utterance 拆 element。
4. 用户触发侧 atom 先扩多条 `source_text`，再把每条 source_text 拆成一组 `trigger_group`。

一个图表 atom 的 element 不是完整句模板，而是对象、动作、属性、事实值、极性、违规动作等短语。例如：

```json
{
  "atom_id": "knowledge:live_latency_low",
  "element_groups": [
    {
      "elements": [
        {"value": "低延迟直播", "main": true, "fact": false, "pool": ["低延时直播", "低延迟模式"]},
        {"value": "延迟", "main": true, "fact": false, "pool": ["时延", "延迟时间"]},
        {"value": "1-2秒", "main": false, "fact": true, "pool": ["一到两秒", "1到2秒"]}
      ]
    }
  ]
}
```

这里 `main` 负责把候选证据召回，`fact` 负责事实精判，`pool` 负责中文口语等价表达。

### 7.2 DialogueAtom：真实对话的局部证据建模

`ElementEngine.build_atoms()` 会把 `EvidenceUnit` 转成多个 `DialogueAtom`。这一步是运行时本地完成的，不调用 LLM，也不读取正负包标签。

每个对话原子包含：

| 字段 | 含义 |
|---|---|
| `atom_id` | 本地生成的对话片段 ID。 |
| `turn_index` | 所属轮次。 |
| `speaker` | user / assistant。 |
| `text` | 当前局部片段文本。 |
| `span_type` | 整轮、子句或相邻窗口片段。 |
| `elements` | 本地抽取的通用元素，例如 speaker_role、question、driving、unavailable。 |

代码会先保留整轮文本，再按中文逗号、句号、问号、分号，以及“然后、另外、还有、同时、接下来”等连接词切出局部子句。这样做的目的有两个：

1. **避免混证据**：同一轮里前半句安全、后半句违规时，hard 判断只看同一个局部片段，不把两边错误拼接。
2. **支持自然拆句**：客服把一个节点 atom 拆到相邻短句里时，node_positive 可以在相邻窗口里合并判断。

例如客服说：

```text
标准直播费用更低一些，延迟大概 5 到 10 秒；低延迟直播互动更流畅，大概 1 到 2 秒。
```

本地会形成若干 DialogueAtom：

```text
assistant_turn_3_whole
assistant_turn_3_clause_1：标准直播费用更低一些
assistant_turn_3_clause_2：延迟大概 5 到 10 秒
assistant_turn_3_clause_3：低延迟直播互动更流畅
assistant_turn_3_clause_4：大概 1 到 2 秒
```

后续匹配不是对整轮一刀切，而是在这些局部原子和相邻窗口中寻找证据。

### 7.3 DialogueAtom 如何本地 elements 化

对话原子的 elements 不是业务知识库，也不是从数据集标签生成，而是由 `_generic_elements()` 加入跨业务通用语义，例如：

| 通用元素 | 触发文本例子 |
|---|---|
| `speaker_role=assistant/user` | 说话人。 |
| `form=question` | “吗、是否、能不能、？”等。 |
| `intent=confirm` | “确认、核实、请问、是不是”。 |
| `context_state=unavailable` | “忙、不方便、没时间、稍后”。 |
| `context_state=driving` | “开车、骑车、路上、安全”。 |
| `context_state=refusing` | “不想、算了、不做、拒绝”。 |
| `intent=close` | “不打扰、再联系、祝、再见、结束”。 |
| `intent=continue_push` | “继续、接着、简单说、说完”。 |

这些元素只描述通用中文客服话语行为，不写入“飞毛腿”“低延迟直播”等业务事实。业务对象和事实值仍然来自图表 atom 的 element 和 pool。

运行时实际匹配有两层来源：

```text
DialogueAtom 自带的通用 elements：用于问句、开车、忙碌、拒绝、结束、继续推进等通用语义。
DialogueAtom.text 原文：用于和图表 element.value / aliases / pool 做表面或保守模糊匹配。
```

也就是说，系统不会先把所有对话业务词预先做成固定词典；它只把对话切片和通用状态元素准备好，业务语义由图表 atom 侧的 element 来驱动匹配。

### 7.4 候选召回：图表 main / pool 先找相关 DialogueAtom

`_recall_candidates()` 是图表 atom 与 DialogueAtom 对齐的第一关。它只使用图表 element 中 `main=true` 的元素及其 `pool` 召回候选 DialogueAtom：

```text
图表 main element / pool
→ 扫描 DialogueAtom.text 和 DialogueAtom.elements
→ 找到可能相关的局部证据片段
→ 形成候选证据池
```

非 main 元素、fact-only 元素不能单独召回候选。这样可以防止“可以、情况、问题、确认”这类辅助词把大量无关句子拉进来。

例如图表 atom 是：

```text
询问发布方式：Web 控制台 / 第三方系统 / SaaS 系统
```

召回时会优先看：

```text
发布方式、Web、控制台、第三方系统、SaaS、系统发课
```

而不会因为对话里出现“可以吗”“这个情况”就召回。

### 7.5 pool 不是给对话造词，而是在候选内展开命中

需要特别说明：`pool` 属于图表 element，不属于数据集标签，也不是运行时给 DialogueAtom 生成业务同义词。代码的顺序更准确地说是：

```text
先把对话切成 DialogueAtom
→ 再用图表 main / pool 召回候选 DialogueAtom
→ 进入候选窗口后，对每个图表 element 展开 value / aliases / pool
→ 在 DialogueAtom.text 或通用 elements 中判断是否命中
```

因此，“pool 化”并不是把每句对话扩写成很多同义句，而是把图表端已经生成好的等价表达池用于命中判断。例如：

```json
{"value": "稍后再打", "pool": ["稍后联系", "过会儿再打", "回头再联系"]}
```

候选 DialogueAtom 里只要出现“过会儿我再联系您”，就可以命中这个图表 element。

### 7.6 局部窗口：候选不是只能在同一个短句里完成

一个 element_group 不一定只在同一个短片段里完成。`candidate_windows()` 会在候选 DialogueAtom 周围构造几类窗口：

| 窗口 | 系数 | 用途 |
|---|---:|---|
| `same_atom` | 1.00 | 同一短片段。 |
| `same_turn_adjacent_2` | 0.92 | 同一轮相邻片段。 |
| `same_turn_adjacent_3` | 0.82 | 同一轮更宽相邻片段。 |
| `same_speaker_adjacent_turn_2` | 0.96 | 节点履约可跨相邻客服轮。 |
| `same_speaker_adjacent_turn_3` | 0.90 | 节点履约更宽跨轮。 |
| `context_carry_over` | 0.75 | 上下文承接。 |

节点履约可以适度使用相邻窗口，因为真实客服可能把“对象”和“动作”拆开说。知识和 hard 限制更谨慎：尤其 hard constraint 不使用相邻窗口，只看 `same_atom`，避免把一句“不能承诺”和另一句“优惠券”错误拼成违规或安全。

### 7.7 元素如何命中文本

核心函数是 `element_hit()` 和 `text_hit()`。一个图表 element 命中 DialogueAtom，有三条路径：

1. DialogueAtom 自带通用 element 与图表 element 的类型和值一致。
2. 图表 `element.value`、`aliases`、`pool` 中任一表达出现在 `DialogueAtom.text`。
3. 长中文短语可以使用保守字符重叠兜底，但短词、数字、时间、金额、单量、秒数不走模糊重叠。

`text_hit()` 的关键保护是：

```text
如果元素里有数字，或包含“元、点、天、单、秒、分钟、小时”等事实单位，必须精确表面命中或通过 pool 命中；
不能用字符重叠把“10天”误判成“10单”。
```

### 7.8 元素组内部如何算分

`_eval_group()` 会计算两个覆盖率：

```text
main_cov  = main 主元素命中权重 / main 主元素总权重
group_cov = 全部元素命中权重 / 全部元素总权重
score     = 0.70 * main_cov + 0.30 * group_cov
```

语义上就是：主干决定是否相关，辅助元素决定是否充分。一个候选 DialogueAtom 只命中对象但没有命中动作、属性或事实值，通常只能得到 partial/review，不能直接当成完整履约或正确事实。

### 7.9 元素权重怎么来

`_element_weight()` 的逻辑：

```text
main=true        → 权重乘 1.5
fact=true        → 再乘 1.25
本地 IDF         → 越少见的元素权重越高
特殊元素因子     → 防止泛词权重过高
最终权重限制在 0.2 到 4.0
```

也就是说，“低延迟直播、前一天18点、优惠券、开车”这类具体对象比“问题、情况、可以”更有区分度。

### 7.10 fact 元素的开关作用

在知识和限制侧，`fact=true` 是确定性开关：

1. 如果有 fact 元素但 fact 没命中，直接 miss。
2. fact 必须绑定到同组非 fact 主干，不能单独命中。
3. 例如“10单”不能脱离“多日合同/每天完成”主干去命中另一个知识项。

这就是当前版本解决知识误杀的核心之一。

### 7.11 用户触发组的特殊规则

用户 trigger 的目标不是判断客服履约，而是决定某个节点是否进入激活子图。

代码中 `_trigger_main_clusters()` 会做两件事：

1. 把“X说忙 + 忙”“在开车 + 开车”这种嵌套重复主干合并成一个状态簇。
2. 去掉纯参与者锚点，例如“我、你、用户、客户、老板、负责人、客服”。

如果一个触发组只命中“我”或“用户”，而没命中“不想配送、开车、不知情、已设置费用”等判别状态，就不能触发分支。

### 7.12 命中结果三态

元素层输出 `ElementMatch`，三态为：

| verdict | 含义 |
|---|---|
| `hit` | 本地命中，可以直接作为证据。 |
| `review` | 有局部证据但未达命中阈值，可进入灰区候选。 |
| `miss` | 未形成有效证据。 |

不同规则类型有不同阈值。例如：

| rule_type | hit_main | hit_group | review_main | review_group |
|---|---:|---:|---:|---:|
| `node_positive` | 0.52 | 0.46 | 0.28 | 0.22 |
| `node_trigger` | 0.50 | 0.42 | 0.26 | 0.20 |
| `knowledge_positive` | 0.52 | 0.58 | 0.30 | 0.34 |
| `constraint_negative` | 0.52 | 0.58 | 0.30 | 0.34 |
| `soft_global` | 0.48 | 0.30 | 0.24 | 0.18 |

知识和限制侧比节点侧更严格，因为它们承担事实和合规判断。

## 8. 节点激活与激活子图评分

对应 `graph_evaluator.py`。

### 8.1 总体 evaluate 流程

`GraphEvaluator.evaluate()` 主要流程：

```text
读取 dialogue turns
→ EvidenceExtractor.extract()
→ ElementEngine.build_atoms()
→ _resolve_context() 处理终止策略
→ _evaluate_node() 逐节点评分
→ _resolve_structural_transitions() 处理 terminal/suppress 边
→ KnowledgeJudge.judge()
→ ConstraintJudge.judge()
→ _score() 计算四维分和总分
→ 返回 EvaluationResult
```

### 8.2 节点是否 active

`_node_active()` 的核心原则：

```text
节点是状态，不是扁平清单。
主线状态默认 active；分支、FAQ、越界、终止状态只有被触发才 active。
```

具体规则：

| 情况 | active 逻辑 |
|---|---|
| `main/start/normal` + `always` | 默认 active。 |
| `condition/user_triggered` | 用户 trigger 命中才 active。 |
| `faq/out_of_scope` | 用户 trigger 命中，或客服确实回答了该节点时 active。 |
| `terminal` | 必须由用户/上下文 trigger 命中，不能由客服关闭话术自激活。 |
| 未触发分支 | 标为“不适用”，不扣分。 |

这就是“激活子图评分”的基础。

### 8.3 FAQ / branch 的 atom 级路由

代码中历史函数名 `_scoped_requirements_for_node()` 仍然保留，但当前语义已经不是旧方法的 requirement/evidence group，而是对节点内部 `node_atom` 做范围筛选。它会避免一个 FAQ 节点被触发后要求所有 sibling atom 都命中。

例如用户只问“怎么退出飞毛腿”，只应该评退出相关 atom，不应该同时要求奖励、单量、名额、天气等 FAQ atom。

它会根据：

1. 用户最近问题型话术。
2. trigger text。
3. atom 的元素文本。
4. 用户文本与 atom 文本的相似度。

来保留相关 atom，其他 atom 作为 skipped，不当成缺失。

### 8.4 用户已提供信息则不重复问

`_user_already_provided_info()` 处理信息获取类节点。

如果 atom 是“询问发布方式 / 确认是否已设置费用 / 确认当前号码是否可加企微”，而用户已经主动说了相关状态，本地不会强制要求客服重复问一遍。

这解决了真实电话中常见的情况：用户先说“我们用 SaaS 发课”“费用已经设置了”，客服直接用这个信息继续处理是合格的。

### 8.5 节点 atom 如何评分

代码中 `_evaluate_requirement()` 和 `RequirementResult` 属于历史兼容命名；当前真正评分对象是节点内部的 `node_atom`。它会用图表 atom 的 `element_groups` 去匹配 assistant 侧 DialogueAtom。

步骤：

```text
取 node_atom.element_groups（兼容旧字段名 requirements）
→ 构造 rule_type=node_positive 的 ElementRule
→ 只扫描 assistant DialogueAtom
→ 先用 main / pool 召回候选 DialogueAtom
→ 在候选窗口内展开 value / aliases / pool 命中元素
→ ElementEngine.match_rule() 输出 hit/review/miss 和 score
→ 写入 element_audit
```

这里非常重要：

```text
ElementEngine 已经判断 hit/review/miss。
GraphEvaluator 不再因为连续分数低于全局 node_satisfied 就把 hit 改成 miss。
```

低置信 hit 会保留审计信息，但不重复误杀。

### 8.6 节点分如何算

对一个 active 节点：

```text
节点分 = 必需 atom 分数的加权平均
```

如果一个节点有多个必需 atom，且某个必需 atom 分数为 0，会触发节点上限：

```text
node_required_atom_missing_cap 默认约 0.58
```

这样可以防止“一个大节点里说了一堆别的内容，但漏掉一个核心 atom”仍然拿高分。

节点状态：

| 状态 | 条件 |
|---|---|
| `已完成` | score >= `node_satisfied`，默认 0.75。 |
| `部分完成` | score >= `node_partial`，默认 0.35。 |
| `缺失` | 低于部分完成阈值。 |
| `不适用` | 未触发或被终止策略压制。 |

---

## 9. 关系、转场和终止策略

### 9.1 terminal policy

`terminal_policies` 描述异常状态触发后哪些节点不再要求。

例如用户开车：

```text
触发节点：用户开车
客服安全处理：稍后再打 / 结束通话
压制节点：后续主线说明、费用确认、企业微信跟进等
```

如果客服正确终止，后续节点会被标为“不适用”；如果客服继续推进，可能触发上下文事件或 hard 违规。

### 9.2 structural transition

`_resolve_structural_transitions()` 会读取 `terminal_after / suppress_after` 边。如果终止/压制边触发，相关下游节点不再强制评分。

### 9.3 关系分

`_relation_score()` 检查：

1. 前置节点是否缺失。
2. 后续节点是否缺失。
3. 后置节点是否早于前置节点。
4. 条件分支是否在目标节点 active 时有前置路径。
5. terminal 后是否仍继续进入被压制节点。
6. relation_groups 是否满足 sequential / exclusive_branch / any_of 等结构。
7. atom_relations 是否满足 atom 级 before、any_of、all_of、condition_on。

现在关系扣分不会直接硬阻断正包；它先进入 relation_score，再由正包组件分阈值统一判断。这样避免自然电话顺序小变动导致完美图正包失败。

---

## 10. 知识表核验：如何判断事实正确或冲突

对应 `knowledge_judge.py`。

### 10.1 知识判断只扫描客服话

知识表判断的是客服有没有说错事实，因此 `KnowledgeJudge.judge()` 只取 `speaker == assistant` 的对话原子。

### 10.2 selector → correct/wrong 的判断流程

对每条知识：

```text
1. 用 selector_groups 召回候选客服话。
2. 如果 selector miss，但 value_check 或方向冲突能独立严格判断，也允许继续检查。
3. 在被 selector 选中的局部话语里检查 correct_groups。
4. 同时检查 wrong_groups。
5. 如果 wrong hit → 冲突。
6. 如果 correct hit 且 wrong miss → 支持。
7. 如果只有 partial/review → 证据不足或不生成事件。
```

### 10.3 value_check

`value_check` 用于数字、时间、金额、单量、时长等可比较事实。

典型结构：

```json
{
  "checks": [
    {
      "field": "低延迟直播延迟",
      "expected_value": "1-2",
      "unit": "秒",
      "condition": "低延迟直播场景",
      "slot_anchors": ["低延迟直播", "延迟"]
    }
  ]
}
```

执行逻辑：

1. 先确认 `slot_anchors` 在同一知识对象范围内。
2. 抽取对话中的数字和单位。
3. 比较 expected_value 和实际值。
4. 对“18点 / 18:00 / 下午6点”这类时间做通用归一。
5. 对“5到10秒 / 5-10秒”做范围比较。

如果没有可比较值，不能伪造 value_check。非数值方向事实应该走 wrong_groups 或 directional conflict。

### 10.4 方向性冲突

`_judge_directional_fact_conflict()` 用于：

```text
高 / 低
多 / 少
便宜 / 更贵
适合 / 不适合
已 / 未
可以 / 不可以
有助于 / 无关
前 / 后
今天 / 明天 / 次日 / 立即
```

它会要求方向词绑定到同一知识对象或同一子句，避免把正确对比误杀。

例如：

```text
标准直播费用较低，低延迟直播费用略高。
```

这里“较低”和“略高”属于不同对象，不能互相判冲突。

### 10.5 知识输出

知识事件字段：

| 字段 | 含义 |
|---|---|
| `verdict` | 支持 / 冲突 / 证据不足 / 未提及。 |
| `evidence` | 命中的客服原话。 |
| `positive_verdict` | correct side 的元素命中状态。 |
| `negative_verdict` | wrong side 的元素命中状态。 |
| `element_audit` | correct/wrong 两侧的命中元素、缺失元素、候选窗口。 |

只有 `verdict == 冲突` 的知识事件会进入硬错误列表并影响知识分和负包验收。

---

## 11. 限制表核验：如何判断 hard / soft

对应 `constraint_judge.py`。

### 11.1 hard 判断流程

对硬限制：

```text
1. 判断是否需要用户 trigger。
2. 如果需要 trigger，先用 constraint_trigger 扫 user 话术。
3. 确定候选对话范围。
4. 默认扫描 assistant 话术。
5. 匹配 negative_groups。
6. 只在同一个候选 atom 内匹配 safe_groups。
7. negative hit 且 safe miss → 违规。
8. safe hit 且 negative miss/review → 安全。
9. negative hit 且 safe hit → 证据不足，需要仲裁。
10. negative miss → 安全，不制造灰区扣分。
```

这个逻辑强调：hard 是负向对象检查。安全侧只能在同一句或同一候选内抵消违规，不能用另一句“我不能承诺”去抵消后面一句“但我保证给您优惠券”。

### 11.2 hard 例子

违规：

```text
我帮您申请优惠券，肯定能减免费用。
```

命中：

```text
negative_groups:
- 优惠券 / 折扣券
- 保证 / 承诺 / 肯定 / 帮您申请
```

安全：

```text
优惠券这边我不能承诺，具体以平台规则为准。
```

命中：

```text
safe_groups:
- 不能承诺
- 以平台规则为准
```

### 11.3 soft 判断流程

软限制不查某个明确业务对象，而是统计 assistant 话术中软质量元素或结构指标。

两类实现：

1. `structural_metric`：例如单轮最大字数、近邻回复重复度。
2. `soft_global_element_statistics`：统计某类软元素在客服话术中的比例。

soft 输出 `安全` 或 `软问题`，只影响 `soft_constraint_score`，不会作为 hard 违规。

---

## 12. 四维分数和上限机制

`GraphEvaluator._score()` 汇总五个维度：

| 维度 | 默认权重 | 来源 |
|---|---:|---|
| 节点完成度 | 0.50 | active 节点和 atom 得分。 |
| 结构关系 | 0.10 | edges、relation_groups、atom_relations、terminal/suppress。 |
| 知识正确性 | 0.20 | 知识冲突事件。 |
| 硬限制合规 | 0.15 | hard constraint 违规事件。 |
| 软限制质量 | 0.05 | soft constraint 统计。 |

基础总分是加权和。

严重错误会触发上限机制：

| 错误 | 影响 |
|---|---|
| 核心流程缺失 | 限制最终分数上限，防止其他内容拉高。 |
| 知识冲突 | 压低知识分和总分上限。 |
| hard 违规 | 压低限制分和总分上限。 |
| 终止后继续推进 | 产生上下文/关系扣分，可能进入限制或仲裁。 |

---

## 13. 正负包严格验收

对应 `dataset_interface.py`。

### 13.1 正包验收

正包不是“总分高就行”。当前正包必须同时满足：

1. 总分达到 `positive_pass`，默认 90。
2. 节点完成分达到组件阈值，默认 78。
3. 关系分达到组件阈值，默认 70。
4. 知识分达到组件阈值，默认 98。
5. 限制分达到组件阈值，默认 98。
6. 没有知识冲突。
7. 没有 hard 违规。
8. 没有 active 必需节点缺失。
9. 没有 active 必需 atom 未命中。
10. 没有未处理的上下文终止/转场问题。

低置信 hit 现在主要作为审计信息，不直接阻断正包，因为 ElementEngine 已经给出了 hit。

### 13.2 负包验收

负包通过条件：

```text
预设错误被本地 evaluator 或仲裁识别
且无关严重误杀数不超过阈值
```

当前默认：

```json
"negative_max_unexpected_bad_events": 0
```

也就是负包应该只错在预设错误那里，不应该把大量无关节点、FAQ、关系、知识一起误杀。

负包验收不会用 `wrong_statement` 直接产生评分事件，只会在 evaluator 已经产生事件之后做对齐。

### 13.3 unexpected_bad_events

负包额外误杀主要检查：

1. 未对齐的知识冲突。
2. 未对齐的 hard 违规。
3. 未对齐的 active 必需流程缺失。
4. 严重上下文转场问题。

关系扣分通常已经进入 relation_score，不再单独硬阻断负包，否则自然顺序波动会造成过量误杀。

---

## 14. 分数同步 `score_adjuster.py`

验收层判断“样本是否通过”，分数层判断“客服表现分”。

`apply_dataset_score_adjustments()` 的作用是：当负包预设错误被确认，或正包出现严重硬错误时，让最终总分、验收结果和报告口径一致。

它不是作弊式根据标签打分，而是把已经由本地 evaluator / 仲裁确认的问题同步到最终结果，避免报告出现“严重错误但高分”的冲突。

---

## 15. 灰区候选、本地二筛和 LLM 仲裁

### 15.1 OracleRouter

`oracle_router.py` 只生成候选，不直接调用模型。

候选来源：

1. node_atom 元素命中灰区。
2. 知识证据不足。
3. 限制正负侧同时命中。
4. 上下文转场灰区。
5. 负包预设错误本地未完全命中但可仲裁。

每个候选必须有：

```text
schema 锚点：node_id / atom_id / knowledge_id / constraint_id
对话证据：具体客服原话或用户触发原话
局部问题：要判断的单一事项
```

### 15.2 LocalSecondFilter

`local_second_filter.py` 先二筛：

| 小机制 | 作用 |
|---|---|
| `schema_anchor_score` | 候选是否绑定明确图表对象。 |
| `evidence_anchor_score` | 候选是否有具体原话。 |
| `same_evidence_ledger_relation` | 已被账本明确支持或安全的同证据不重复送审。 |
| `local_strict_promotion` | 本地已能确认的问题不送 LLM。 |
| 候选合并 | 同类同证据只保留代表项。 |
| 低价值过滤 | 空证据、无锚点、弱候选不送。 |

这一步保证 LLM 只处理少量局部语义难题。

### 15.3 LLMVerifier

`llm_verifier.py` 的 payload 只包含：

1. 局部候选问题。
2. schema 锚点摘要。
3. evaluator 账本。
4. 相关原话。
5. 验收上下文。

它不让 LLM 重评整段对话，也不把 `wrong_statement` 当标准答案。

仲裁返回：

| verdict | 含义 |
|---|---|
| `confirmed_issue` | 问题成立。 |
| `rejected_issue` | 问题不成立。 |
| `uncertain` | 证据不足。 |

在 assist 模式下，负包灰区问题可以通过仲裁变成“仲裁通过”。

---

## 16. 报告生成

`report_explainer.py` 和 `report_html.py` 会把结构化结果翻译成中文报告。

当前报告不仅展示分数，还应展示：

1. 节点 active / skipped / suppressed。
2. 用户触发证据和 trigger_groups。
3. 每个 atom 的 element_audit。
4. 命中元素、缺失元素、表达池命中项。
5. 知识 correct/wrong/value_check 侧的命中情况。
6. hard negative/safe 侧的命中情况。
7. 负包 expected error 是否命中。
8. unexpected_bad_events 是否过多。
9. 是否本地通过或仲裁通过。
10. token 和耗时统计。

这使报告能解释“为什么正包没通过”“为什么负包是预期错误命中但误杀过多”“为什么某条样本需要仲裁”。

---

## 17. 交付前质量检查

推荐交付前运行：

```bash
PYTHONPATH=src python tools/method_contract_guard.py
PYTHONPATH=src python tools/anti_leak_guard.py
PYTHONPATH=src python tools/hardcode_guard.py
PYTHONPATH=src python tools/negative_purity_check.py data/dialogues/negative_pack
PYTHONPATH=src python tools/self_check_atom_element_logic.py
PYTHONPATH=src python tools/graph_load_smoke.py
PYTHONPATH=src python tools/constraint_table_smoke.py
PYTHONPATH=src python tools/role_aware_element_smoke.py
PYTHONPATH=src python tools/element_bug_regression_smoke.py
PYTHONPATH=src python tools/activation_scope_smoke.py
PYTHONPATH=src python tools/final_tightener_smoke.py
```

这些工具分别检查：

| 工具 | 作用 |
|---|---|
| `method_contract_guard.py` | 方法契约是否被破坏。 |
| `anti_leak_guard.py` | 负包答案字段是否泄漏进评分。 |
| `hardcode_guard.py` | 是否出现业务硬编码。 |
| `negative_purity_check.py` | 负包标注是否完整。 |
| `self_check_atom_element_logic.py` | atom/element 基本逻辑是否成立。 |
| `role_aware_element_smoke.py` | 用户触发文本扩张与客服元素池合并是否正常。 |
| `element_bug_regression_smoke.py` | “我”等泛词不能单独触发、hard 重复去重等回归检查。 |
| `activation_scope_smoke.py` | 激活子图评分是否避免未触发分支误杀。 |
| `final_tightener_smoke.py` | 最终收紧层是否工作。 |

---

## 18. 当前方法和旧方法的本质区别

| 维度 | 旧方法 | 当前方法 |
|---|---|---|
| 图表结构 | 状态图 + requirement/evidence group | 一图两表 + 评估原子登记 + 语义元素层 |
| 语义匹配 | 关键词/证据组为主 | main/fact/pool 元素组 + 局部窗口 + 本地 IDF 权重 |
| 用户触发 | 抽象 trigger 文本 | source_text → elements → OR trigger_groups |
| 客服履约 | 任务语义切分 | 最可能客服答话 → element → pool |
| 知识判断 | support/refute | selector/correct/wrong/value_check/方向冲突 |
| 限制判断 | 统一 constraint_table | hard/soft 分表，negative/safe 同对象判断 |
| 评分范围 | 容易评整张图 | 只评激活子图 |
| 正包验收 | 总分较主导 | 总分 + 组件分 + 无硬错误 |
| 负包验收 | 预期错误命中即可 | 预期错误命中 + 无关误杀受控 |
| 仲裁 | 灰区辅助 | 本地二筛后的局部裁判 |

---

## 19. 方法总结

当前 ATLAS-Eval 版本可以概括为：

```text
复杂客服指令
→ 运行时配置的 LLM 五阶段离线建图
→ 状态主图、知识表、硬限制表、软限制表
→ 图表评估原子登记
→ 客服侧 expected utterance element 化
→ 用户侧 source_text trigger element 化
→ 真实对话切成 DialogueAtom 并补通用 elements
→ 图表 main / pool 召回 DialogueAtom 候选
→ 候选窗口内做元素命中与精判
→ 本地激活子图评分
→ 知识 selector/correct/wrong/value_check 核验
→ hard negative/safe 同对象核验
→ 正负包严格验收和误杀控制
→ 本地二筛后少量 LLM 仲裁
→ 中文可解释报告
```

它的工业可行性来自三点：

1. **高成本理解前置**：LLM 主要用于离线建图和少量仲裁。
2. **大规模执行本地化**：节点、知识、限制、验收、报告主要由本地代码完成。
3. **证据链可复查**：每个结论都能追溯到 node、atom、element、trigger、knowledge、constraint 和原始对话文本。
