# 02 方法与代码模块说明

本文档按照一次完整评估的执行顺序，说明 SCEG 的方法、代码模块、关键机制、LLM 参与边界和工业可行性。

## 1. 总体方法

SCEG 的核心思想是：不要让大模型直接给整段对话打分，而是让大模型先把复杂客服指令转化为结构化评估标准，再由本地 evaluator 执行评估。

完整链路如下：

```text
复杂客服指令
→ LongCat-Flash-Lite 离线建图
→ Schema Linter 结构规范检查
→ Schema Compiler 可执行规则编译
→ Dialogue Loader 读取对话
→ Evidence Extractor 抽取证据单元
→ Evidence Matcher 匹配节点证据
→ Graph Evaluator 评估节点、结构、知识、限制、上下文
→ Dataset Interface 做正负包验收
→ Oracle Router 生成灰区候选
→ Local Second Filter 本地二筛
→ 可选 LongCat-Flash-Lite 二级仲裁
→ Report Explainer / HTML 输出中文可解释报告
```

一句话概括：**LongCat 离线建标准，本地跑证据，LongCat 只补图和裁灰区。**

## 2. LLM 到底参与了哪些部分

系统中 LLM 只参与三类环节。

### 2.1 第一次离线建图

入口在 `demo_runner.build_graph_with_longcat()`，底层由 `longcat_client.LongCatClient` 调用 LongCat。

输入是一段复杂客服指令和 `prompts/latest_schema_graph_prompt.md`。输出不是普通摘要，而是可执行 schema，主要包括：

- `nodes`：客服必须完成的流程节点；
- `requirements`：每个节点下更细的履约小任务；
- `evidence_groups`：每个小任务需要哪些对话证据；
- `edges` 和 `relation_groups`：哪些节点有顺序、依赖、无序组或分支关系；
- `knowledge_table`：客服不能说错的事实；
- `constraint_table`：客服不能越界承诺、违规施压或做不允许的保证；
- `terminal_policies`：用户忙碌、拒绝、不方便、开车等场景如何安全结束或压制后续节点。

这一步利用了大模型理解复杂自然语言指令的能力，但不会逐条评估所有对话。

### 2.2 第二次 schema repair 补图

入口仍在 `demo_runner.build_graph_with_longcat()`，本地先由 `schema_repair_audit.audit_schema_repair_need()` 检查图是否有硬缺口。如果发现节点缺失、ID 无法绑定、知识表为空、限制表缺 `violation_scope`、终止策略缺触发覆盖等问题，系统会调用 `schema_repair_audit.build_repair_instruction()` 生成补图请求，再交给 LongCat 返回一份完整修正后的 schema。

这里要强调边界：本地 audit 只指出“结构缺口”，不在本地补业务答案。业务事实仍由 LongCat 根据复杂指令生成到 graph/schema 中。

### 2.3 可选二级仲裁

入口在 `llm_verifier.apply_llm_verifier()`。它只处理本地 evaluator 无法稳定判断的局部候选，不让 LLM 总评整段对话。

候选来源包括：

- 某个 requirement 的证据接近命中但不足以本地确认；
- 某个知识 claim 触发了主题但支持/冲突不稳定；
- 某条限制命中了暧昧区；
- 负包验收项绑定到了 schema，但本地 strict 判断没有形成明确事件。

二级判断有三种模式：

| 模式 | 作用 |
| --- | --- |
| `off` | 完全关闭，不调用 LongCat。 |
| `shadow` / 审计模式 | 发送灰区候选，记录 LongCat 判断，但不改最终验收结果。 |
| `assist` / 辅助模式 | 只在 LongCat 或本地二筛确认待仲裁负包问题成立时，把结果改为“仲裁通过”。 |

## 3. `longcat_client.py`：LongCat 调用与 JSON 稳定化

`LongCatClient` 负责连接 LongCat API。默认模型是 `LongCat-Flash-Lite`，默认 base url 是 `https://api.longcat.chat/openai`。

关键机制包括：

1. **模型选择与兼容**：默认使用 `LongCat-Flash-Lite`；如果平台接口侧模型名不可用，客户端有 fallback 候选，避免因为模型名变化导致整个 demo 无法运行。正式展示文档按 `LongCat-Flash-Lite` 说明。
2. **多传输方式**：优先使用 `urllib`，必要时可用 `curl` 兜底。
3. **不限应用层超时**：建图可能比较慢，代码取消固定应用层超时，避免大图生成被提前中断。
4. **JSON 抽取**：`extract_json_object()` 会从 LongCat 返回内容中提取 JSON 对象，支持去除代码块、扫描平衡大括号、修正常见尾逗号和字段间缺逗号等问题。
5. **token 记录**：每次调用都会记录 `prompt_tokens`、`completion_tokens`、`total_tokens`、`calls`，最后写入 `run_token_usage.json`。
6. **模拟回放接口**：当 `SCEG_SIMULATED_LONGCAT_DIR` 被设置时，可以从本地 JSON 文件回放 LongCat 返回，方便离线调试。但正式生产路径仍然是 LongCat API。

这个模块只处理模型调用和 JSON 稳定化，不执行任何客服任务判分。

## 4. `demo_runner.py`：总调度器

`demo_runner.py` 是整个系统的调度中心。两个核心函数是：

| 函数 | 作用 |
| --- | --- |
| `run_project()` | 在线建图 + 评估。对应 `app.py`。 |
| `run_offline_project()` | 读取已有 graph + 评估。对应 `app_offline.py` 和 `scripts/run_offline_graph.py`。 |

### 4.1 在线路径 `run_project()`

执行顺序如下：

1. 读取复杂指令；
2. 加载 `data/dialogues`；
3. 根据指令和对话包生成紧凑 `binding_hints`；
4. 调用 LongCat-Flash-Lite 第一次建图；
5. 执行 schema linter / compiler；
6. 如有硬缺口，调用 LongCat-Flash-Lite 二次 repair；
7. 根据 graph 与 dialogue 的文本兼容度选择对应 domain；
8. 重新编译 graph，把对话包元数据作为 ID alias 绑定；
9. 本地评估每条对话；
10. 可选二级判断；
11. 生成 JSON、HTML、token、timing、manifest 和 upload bundle。

### 4.2 离线路径 `run_offline_project()`

离线路径不会调用 LongCat 建图。它读取已有 graph 后执行：

1. `source_graph.json` 存档；
2. `_legacy_to_latest()` 兼容旧图格式；
3. `compile_state_graph()` 编译 schema；
4. `lint_and_repair_schema()` 做结构检查；
5. 读取本地对话；
6. 根据 graph 和 dialogue 的 domain 兼容度过滤数据；
7. 本地评估；
8. 可选二级判断；
9. 输出报告。

因此 `app_offline.py` 是最适合答辩展示和复现实验的入口。

### 4.3 domain 自动过滤

当 `data/dialogues` 同时包含商家和骑手包时，系统不能把一张商家图套到骑手对话上。`demo_runner._domain_compatibility_filter()` 会优先读取 graph 的 `metadata.domain`，如果没有明确 domain，则用状态图文本和对话文本的字符 n-gram 兼容度做保守过滤。

这不是业务硬编码，因为所有用于匹配的词都来自当前 graph 和当前 dialogue 文件，不是写死在代码里的商家/骑手词典。

## 5. `schema.py`：评估标准的数据结构

`schema.py` 定义了 evaluator 使用的结构化对象：

| 类 | 作用 |
| --- | --- |
| `StateGraph` | 整张状态图，包含节点、边、关系组、知识表、限制表、终止策略。 |
| `GraphNode` | 单个客服动作节点，例如身份确认、通知合同、说明价格等。 |
| `Requirement` | 节点下的履约小任务。一个节点可包含多个 requirement。 |
| `EvidenceGroup` | requirement 的证据组，定义需要匹配哪些客服或用户原话。 |
| `ActivationProfile` | 节点激活条件，区分 always、optional、user_triggered、condition 等。 |
| `GraphEdge` | 节点之间的顺序、依赖、分支关系。 |
| `RelationGroup` | all_of、any_of、ordered 等组关系。 |
| `KnowledgeItem` / `KnowledgeClaim` | 知识表和事实 claim。 |
| `ConstraintRule` | 限制表规则，包括 prohibited、safe_context、trigger、violation_scope 等。 |

这一层把复杂客服指令变成“可执行标准”，后续所有评估都围绕这些结构进行。

## 6. `schema_linter.py`：结构规范检查器

Schema Linter 的目标不是补业务答案，而是保证 LongCat 生成的 schema 能被本地 evaluator 稳定执行。

主要机制包括：

1. **节点检查**：检查 requirement 是否有 evidence group，过软的要求是否不应作为刚性扣分项，分支/FAQ 是否污染主线。
2. **证据组检查**：去除重复 pattern，识别过宽或过窄的 evidence group，避免一个主题词直接完成节点。
3. **知识表检查**：检查 support/refute 是否重叠、是否把答案字段误写入规则、是否缺对象锚点。
4. **限制表检查**：补齐或检查 `violation_scope`，包括 protected_objects、forbidden_actions、safe_actions、ambiguous_zone、trigger_scope。
5. **关系组检查**：检查主流程、可选分支、any_of/all_of 结构是否合理。
6. **反泄漏检查**：移除类似 `wrong_statement`、`evidence_span` 这种负包答案字段进入规则的风险。

Linter 的输出会写入 `schema_linter_report.json` 和 graph metadata，便于报告展示。

## 7. `schema_compiler.py`：结构化规则编译器

Schema Compiler 把 LongCat 输出的自然语言 schema 转换成 evaluator 可执行结构。

主要机制包括：

1. **节点规范化**：统一节点 ID、名称、activation、requirements、evidence groups。
2. **知识表规范化**：整理 `claim_evidence`、support/refute patterns、claim aliases、对象锚点。
3. **限制表规范化**：将 trigger、safe_context、prohibited 和 `violation_scope` 编译成统一 pattern 结构。
4. **ID / alias 绑定**：把数据集中 `target_node_id`、`target_id` 等元数据作为 alias 绑定到图上，解决 LongCat 每次生成 ID 不完全一致的问题。
5. **证据组加固**：对过宽的 any-list 增加 `min_any_hits` 或 window union，使节点完成不依赖单个主题词。
6. **旧格式兼容**：兼容早期图格式和最新 schema 格式。

Compiler 的重要边界是：它可以把当前数据包的目标 ID 作为结构别名，但不能把负包错句或标准答案编译成判分规则。

## 8. `dialogue_loader.py`：对话加载器

`dialogue_loader.load_dialogues()` 负责读取对话 JSON。它支持两类格式：

- 新格式：`turns`；
- 旧格式：`dialogue`。

加载时会统一字段：

- `id`：对话 ID；
- `sample_type`：positive / negative；
- `turns`：多轮对话；
- `domain`：merchant / rider。

它会跳过 graph 文件，只读取真正的对话 JSON。

## 9. `evidence_extractor.py`：证据单元抽取

`EvidenceExtractor` 做的是格式化证据抽取，不做业务语义判断。

每个对话 turn 会被转换成 `EvidenceUnit`，保留：

- turn index；
- speaker；
- 原始文本；
- 规范化文本；
- 显式数字和范围。

数字和范围抽取支持类似：

```text
5秒
5到10秒
1-2天
```

这一层不存任何业务词。它只把对话变成后续 matcher 可以消费的证据单元。

## 10. `evidence_matcher.py`：证据匹配器

`EvidenceMatcher` 是节点履约判断的核心。它根据 schema 中的 evidence group 去匹配对话证据。

支持的通用 pattern 包括：

| pattern 字段 | 含义 |
| --- | --- |
| `speaker` | 匹配客服或用户。 |
| `all` | 当前句必须包含所有值。 |
| `any` | 当前句命中任一值即可。 |
| `min_any_hits` | 至少命中多少个 any 值。 |
| `none` | 出现这些词则排除。 |
| `regex_any` | 正则匹配。 |
| `number` | 数字范围匹配。 |
| `window_union` | 多句合并判断同一证据组。 |
| `cross_turn` | 跨轮次问答，例如客服询问、用户短答确认。 |
| `reply_to_user_any` | 判断客服是否回应了用户上一句。 |

### 10.1 宽证据组加固

LongCat 有时会把一个节点编成很多 `any` 词。如果只命中一个宽泛主题词就算完成，容易误判。代码会对 assistant 侧大 any-list 做加固：

- 默认要求至少两个 any 命中；
- 对重复出现在多个 group 的 broad terms 降权；
- 要求至少有一个非弱主题词命中。

### 10.2 自然中文容错

系统允许少量通用表达容错，例如“知道吗 / 了解吗 / 知情吗”这类知情询问变体。但具体业务对象仍来自 graph，不写在 matcher 中。

### 10.3 跨句合并

客服话术常常为了自然，会把一个要求拆成多句。`_match_pattern_across_turns()` 允许同一 speaker 的多句共同满足 `min_any_hits`，但具体词仍完全来自 schema。

### 10.4 跨轮确认

`cross_turn=assistant_ask_user_affirm` 用于表达“客服问一句，用户短答确认”。本地只识别通用短确认，例如“是的”“嗯”“对”，不写业务答案。

## 11. `graph_evaluator.py`：状态图评估器

`GraphEvaluator` 是本地评估主干。它一次评估会生成四类结果：

1. 节点履约结果；
2. 结构关系结果；
3. 知识核验结果；
4. 限制合规结果；
5. 上下文转场结果。

### 11.1 节点激活

节点 activation 分为：

| 模式 | 含义 |
| --- | --- |
| `always` | 主线必评节点。 |
| `optional` | 默认不评，通常用于可选动作。 |
| `user_triggered` | 用户触发才评，例如用户问额外奖励。 |
| `condition` | 条件满足才评，例如忙碌、开车、拒绝配送。 |

节点未触发时会标记为“不适用”，不作为缺失扣分。

### 11.2 requirement 与 evidence group 评分

一个节点包含多个 requirement，一个 requirement 包含多个 evidence group。评分顺序是：

```text
evidence group 是否命中
→ requirement 分数
→ node 分数
→ node 状态：已完成 / 部分完成 / 缺失
```

默认阈值来自 `config/default_runtime.json`：节点满足阈值通常为 0.75，部分完成阈值为 0.35。

### 11.3 结构关系评分

`_relation_score()` 会检查：

- strict_order 前置节点是否缺失；
- 后续节点是否缺失；
- 后置节点是否早于前置节点出现；
- relation_group 是否满足 all_of、any_of、ordered 等要求。

结构关系不是简单看节点数量，而是结合节点首次命中轮次和图结构计算。

### 11.4 上下文转场

`_resolve_context()` 处理 terminal policies。例如：

- 用户开车：客服应礼貌结束，不再继续主线；
- 用户忙：客服应简短说明，后续再联系或继续简短话术；
- 用户坚持拒绝：客服应安慰后结束，不继续施压。

如果客服正确处理终止策略，后续不适用节点会被 suppress，避免把“不该继续说的主线内容”当成缺失。

### 11.5 总分和 cap

最终分数由四维构成：

| 维度 | 默认权重 |
| --- | ---: |
| 节点完成度 `node_completion` | 0.50 |
| 结构关系 `relation_score` | 0.15 |
| 知识正确性 `knowledge_score` | 0.20 |
| 限制合规性 `constraint_score` | 0.15 |

除了加权平均，系统还会应用 cap。比如：

- 高风险限制违规会压低总分上限；
- 知识冲突和流程缺失叠加会压低上限；
- 多个核心履约证据缺失会压低上限；
- 负包预设错误命中后，`score_adjuster.py` 会进一步打上负包 cap。

cap 的意义是：客服任务成功是门槛，不能因为其他话术说得多就把核心错误平均掉。

## 12. `knowledge_judge.py`：知识事实判断

`KnowledgeJudge` 处理 `knowledge_table`。它输出三态：

| verdict | 含义 |
| --- | --- |
| `支持` | 对话说法和知识表一致。 |
| `冲突` | 对话说法与知识表相反。 |
| `证据不足` | 对话触及该知识主题，但本地证据不足以稳定判断。 |

### 12.1 claim evidence 模式

最常见模式是 `claim_evidence`：每个知识项包含一个或多个 claim，每个 claim 有 support patterns 和 refute patterns。

判断逻辑是：

1. 先检查明确 refute；
2. 再检查 schema 派生的通用冲突，例如否定 support、前后/高低/可不可等方向反转；
3. 再检查 support；
4. 如果 claim 被触发但没有 support/refute，则输出“证据不足”，交给灰区候选。

### 12.2 对象锚点和兄弟知识隔离

同一个任务里可能有多个相邻知识点。代码会从当前 item/claim 的 ID、名称、alias、claim_patterns 中抽取对象锚点，避免一个句子同时误伤兄弟知识。

这些锚点全部来自 schema，不是写死业务词。

### 12.3 数字、范围、方向与时间极性

知识判断还包含一些通用机制：

- 数字范围抽取和重叠比较；
- 高/低、前/后、可/不可、已/未等方向极性保护；
- 时间表达保护，避免“当天/次日/明天”等相邻事实互相污染；
- 占位符和单位冲突检测，例如 X 单、Y 天、W 天一类匿名变量。

这些都是通用语言结构，不对应某个具体业务答案。

## 13. `constraint_judge.py`：限制与合规边界判断

`ConstraintJudge` 处理 `constraint_table`。它也输出三态：

| verdict | 含义 |
| --- | --- |
| `安全` | 命中安全处理方式。 |
| `违规` | 命中禁止动作或越界承诺。 |
| `证据不足` | 命中 ambiguous zone，需要语义仲裁。 |

限制规则主要由 schema 提供：

- `trigger`：什么时候触发限制；
- `safe_context`：怎样说是安全的；
- `prohibited`：哪些表达是禁止的；
- `unresolved`：哪些表达需要仲裁；
- `violation_scope`：结构化违例范围，包括 protected_objects、forbidden_actions、safe_actions、ambiguous_zone。

### 13.1 安全边界说明不会误判为承诺

系统区分两类话：

```text
不能承诺 X，以页面为准        → 安全边界说明
我保证给你 X / 我帮你申请 X  → 可能违规
```

本地只内置“不能、无法、不承诺、保证、确保、一定、帮您、必须、否则”等通用中文话语行为算子；具体 X 是什么，必须来自 schema 的 protected object 或 prohibited pattern。

### 13.2 触发后上下文窗口

有些限制必须在用户触发后才成立。例如用户表示忙、拒绝、不方便，客服后续还继续施压，才构成违规。代码会记录首次触发轮次，并判断触发后的上下文是否仍然有效。

### 13.3 violation_scope 结构化判断

如果 schema 提供 `violation_scope`，系统会执行：

```text
受保护对象命中 + 禁止动作命中 → 违规
安全动作命中 → 安全
暧昧区命中 → 证据不足，进入仲裁候选
```

这样比单纯关键词禁止更适合真实客服边界判断。

## 14. `dataset_interface.py`：正负包验收层

GraphEvaluator 负责“评价对话表现”，DatasetInterface 负责“判断样本是否通过”。

### 14.1 正包验收

正包通过条件主要是：

- 总分达到正包阈值；
- 没有知识冲突；
- 没有限制违规；
- 没有 active 必需节点缺失；
- 没有未处理的上下文转场问题。

如果正包带有 `coverage_targets`，说明它是分支场景样本，不一定要求整条主线全部出现；只要没有事实冲突、限制违规，并覆盖了场景目标，可以按场景样本通过。

### 14.2 负包验收

负包包含 `injected_errors`。验收层会检查预设错误是否被 evaluator 识别。

常见错误族包括：

| error_family | 本地判定来源 |
| --- | --- |
| `flow_missing` | 对应节点或 requirement 缺失。 |
| `knowledge_violation` | 知识表产生冲突事件。 |
| `constraint_violation` | 限制表产生违规事件。 |

重要红线：`evidence_span` 和 `wrong_statement` 只能用于追踪和解释，不能单独让负包通过。也就是说，负包不是因为“答案字段写了错句”而通过，而是因为本地 evaluator 真正识别出了节点缺失、知识冲突或限制违规。

### 14.3 语义灰区升级

如果负包目标绑定到了知识或限制 schema，但本地 strict 判断没有形成确定事件，DatasetInterface 可以把该目标标记为 `oracle_expected`，交给后续 OracleRouter 和 LocalSecondFilter 决定是否进入 LongCat 仲裁。

## 15. `score_adjuster.py`：分数与验收同步

`score_adjuster.apply_dataset_score_adjustments()` 用于把数据集验收和评分 cap 对齐。

典型作用是：

- 负包预设流程缺失命中后，总分不应仍然很高；
- 负包预设知识错误或限制违规命中后，应给出对应 cap；
- 正包场景型样本如果达到 coverage 目标且无硬错误，可以同步验收结果。

这不是业务特判，而是正负包评测协议的统一后处理。

## 16. `oracle_router.py`：灰区候选生成

`OracleRouter` 不直接调用 LLM，它只创建候选队列。候选必须有明确 schema 锚点和对话证据。

来源包括：

1. requirement 覆盖灰区；
2. 知识核验灰区；
3. 限制边界灰区；
4. 上下文转场灰区；
5. 负包样本验收灰区。

每个候选都包含：

- candidate_id；
- kind；
- node_id / requirement_id / knowledge_id / constraint_id；
- question；
- evidence；
- need；
- strength；
- expected_detector。

`oracle_budget` 控制每条对话最多产生多少候选，避免无限送审。

## 17. `local_second_filter.py`：本地二筛

本地二筛是工业可行性的关键。它在真正调用 LongCat 前先做筛选。

主要机制包括：

1. **schema anchor score**：候选是否绑定了 node、requirement、knowledge、constraint 或 context；
2. **evidence anchor score**：候选是否有足够具体的对话证据；
3. **local strict promotion**：本地已经能确认的问题，不再送 LLM；
4. **same evidence ledger relation**：如果同一证据在本地账本中已经是支持/安全，则不重复送审；如果已经是冲突/违规，则可本地确认；
5. **重复候选合并**：同一类候选只发送代表项，减少 token；
6. **无锚点或空证据忽略**：没有 schema 绑定或没有实质证据的候选不会送审。

因此系统不会把所有样本交给 LLM，而是只把少量有价值的局部灰区交给 LongCat-Flash-Lite。

## 18. `llm_verifier.py`：LongCat 二级判断

`llm_verifier.py` 接收本地二筛后的候选，构造一个局部判断 payload。它要求 LongCat 只返回三类结果：

| 结果 | 含义 |
| --- | --- |
| `confirmed_issue` | 问题成立。 |
| `rejected_issue` | 问题不成立。 |
| `uncertain` | 证据不足。 |

payload 只包含：

- 候选问题；
- schema 绑定；
- evaluator 账本；
- 客服实际表达摘要；
- 本地验收上下文。

不会把负包 `wrong_statement` 当作答案交给 LongCat。对于负包仲裁，系统会保留“客服实际表达摘要”，避免 LongCat 只看到本地账本而看不到真实话术。

在 `assist` 模式下，只有当 LongCat 或本地二筛确认待仲裁负包问题成立时，负包才会从“待仲裁”变为“仲裁通过”。

## 19. `report_explainer.py` 与 `report_html.py`：中文可解释报告

报告模块把结构化评估结果翻译成评委能读懂的中文。

输出内容包括：

- 一句话结论；
- 总分和四维分数；
- 正负包验收结果；
- 节点和 requirement 命中情况；
- 证据组期望与命中原话；
- 知识支持/冲突/证据不足；
- 限制安全/违规/证据不足；
- 结构关系事件；
- 上下文转场事件；
- LongCat 仲裁候选和结论；
- token 使用和运行时间；
- schema linter 提示。

报告分为：

| 报告 | 面向对象 |
| --- | --- |
| `report_simple.html` | 非技术评委，重点看结果、通过率、失分归因。 |
| `report_detail.html` | 技术评委，重点看节点、证据、知识、限制和仲裁明细。 |
| `case_reports/*.html` | 单条样本详细追踪。 |

## 20. `tools/`：反硬编码与反泄漏工具

项目保留三个交付检查工具：

```bash
PYTHONPATH=src python tools/hardcode_guard.py
PYTHONPATH=src python tools/anti_leak_guard.py
PYTHONPATH=src python tools/negative_purity_check.py data/dialogues/negative_pack
```

### 20.1 反硬编码

允许本地代码保存通用客服语言算子，例如：

- 知道吗、了解吗；
- 忙、不方便、稍后；
- 不能承诺、以页面为准；
- 必须、否则、我帮您。

不允许本地 evaluator 写入具体业务事实，例如某个商家产品、骑手规则、费用、名额、派单、特殊流程答案等。具体业务内容必须来自 LongCat graph/schema。

### 20.2 反泄漏

负包中的 `wrong_statement`、`evidence_span`、`injected_errors` 是验收追踪数据，不能被编译成 evidence group、support/refute、prohibited pattern。否则系统就会读答案，而不是评估对话。

### 20.3 负包纯度

负包应有明确 injected error，每条负包只破坏核心目标或少量清晰目标，避免错误过于暧昧导致评估不稳定。

## 21. 为什么这个方法工业可行

### 21.1 高成本环节前置到离线建图

复杂客服指令通常远少于待评估对话。SCEG 把最需要大模型理解能力的部分放在离线建图阶段：

```text
复杂指令 → LongCat-Flash-Lite → 状态图/知识表/限制表
```

这份 graph 可以缓存和复用，不需要每条对话都重新让大模型理解复杂指令。

### 21.2 大规模评估由本地执行

正式评估时，节点命中、结构关系、知识判断、限制判断和正负包验收都由本地 Python 执行。它具有：

- 成本低；
- 速度快；
- 可复现；
- 可批量回归；
- 可输出结构化证据。

相比 LLM-as-judge，它不依赖大模型每次主观打分。

### 21.3 只有少量灰区送 LongCat

系统不会把所有样本送给 LongCat。它先通过 `OracleRouter` 创建候选，再通过 `LocalSecondFilter` 做本地二筛，只把有 schema 锚点、有对话证据、语义方向不稳定的少量候选送审。

这使 token 成本可控，也使 LongCat 的作用更像“局部仲裁员”，而不是“全局黑盒裁判”。

### 21.4 报告能展示成本和证据

系统会在报告和运行文件中记录：

- 建图是否命中缓存；
- LongCat 建图调用次数；
- 二次 repair 是否触发；
- token 使用量；
- 二级判断送审候选数量；
- 本地二筛过滤数量；
- 每条样本的证据和归因。

因此工业可行性不是口头声明，而是可以通过运行产物直接展示。

## 22. 方法优势总结

SCEG 的优势可以概括为四点：

1. **结构化**：把复杂客服指令拆成状态图、知识表、限制表和终止策略；
2. **可解释**：每个分数都能追溯到节点、小任务、证据组和原话；
3. **低成本**：大模型主要用于离线建图和少量灰区仲裁，大规模评估本地执行；
4. **可落地**：适合真实客服质检、复杂任务验收和批量回归测试。
