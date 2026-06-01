# 02 方法与代码模块说明：按评估执行顺序

本文档严格按照一次评估从输入到输出的执行顺序，说明 SCEG 的方法、代码模块、LLM 参与边界、状态图特征，以及为什么该方案具备工业可行性。

SCEG 的核心思想是：**不要让大模型直接对整段对话打黑盒分，而是先让 LongCat 把复杂客服指令离线转成可执行的状态主图、知识副表和限制副表，再由本地 evaluator 对多轮对话进行证据化、结构化、可解释评估。**

完整执行顺序如下：

```text
复杂客服指令 / 离线 graph
→ LongCat-Flash-Lite 离线建图或读取已有 graph
→ schema_repair_audit 判断是否需要二次补图
→ Schema Linter 做结构规范检查
→ Schema Compiler 编译成可执行评估结构
→ Dialogue Loader 读取正负包对话
→ domain compatibility filter 选择与 graph 匹配的业务域
→ Evidence Extractor 把多轮对话转成证据单元
→ Evidence Matcher 用主图证据组匹配流程节点
→ Knowledge Judge 用知识副表核验事实正确性
→ Constraint Judge 用限制副表核验合规边界
→ Graph Evaluator 综合节点、关系、上下文转场、知识事件、限制事件和四维分数
→ Dataset Interface 做正负包验收
→ Score Adjuster 同步总体验收上限机制与验收结论
→ Oracle Router 生成灰区候选
→ Local Second Filter 做本地二筛和候选合并
→ 可选 LongCat-Flash-Lite 二级仲裁
→ Report Explainer / Report HTML 输出中文可解释报告
→ demo_runner 汇总运行产物、token、耗时和 upload bundle
```

其中，LLM 只出现在三个位置：**离线建图、必要的二次补图、少量灰区仲裁**。节点命中、知识核验、限制判断、正负包验收和报告生成主体都在本地完成。

这里需要特别说明：知识副表和限制副表并不是最终报告前的附加项，而是在证据抽取和主图节点匹配之后立即参与本地核验。它们的结果会提前进入综合评分、验收上限、本地二筛和仲裁候选生成。

## 1. 输入层：复杂指令、对话包和运行配置

一次评估需要三类输入。

| 输入 | 来源 | 作用 |
| --- | --- | --- |
| 复杂客服指令 | `prompts/instructions/` 或界面输入 | 告诉系统客服应当完成什么流程、不能说错哪些事实、不能越过哪些边界。 |
| 对话数据 | `data/dialogues/positive_pack` 和 `data/dialogues/negative_pack` | 被评估的多轮客服对话。正包测试能否放过合格对话，负包测试能否识别预设错误。 |
| 运行配置 | `config/default_runtime.json` | 保存评分权重、节点阈值、总体验收上限参数、仲裁预算和通用中文话语行为算子。 |

运行配置只保存通用机制，例如节点完成阈值、四维权重、候选预算、通用承诺/拒绝/忙碌表达等，不写入商家或骑手的具体业务答案。具体业务事实必须来自 LongCat 生成的 graph/schema。

## 2. 第一步：LongCat 离线建图

对应模块：

```text
longcat_client.py
schema_repair_audit.py
demo_runner.build_graph_with_longcat()
prompts/latest_schema_graph_prompt.md
```

在线模式下，系统首先把复杂客服指令发送给 LongCat-Flash-Lite。LongCat 输出的不是普通摘要，而是一份可执行 schema。该 schema 会成为后续本地 evaluator 的评估标准。

### 2.1 LongCat 建图输出什么：一主图、二副表、终止策略

LongCat 建图的输出不是把节点、知识点、限制点简单并列罗列，而是按照 **“一主图、二副表”** 的层次组织成可执行 schema。

```text
复杂客服指令
└─ 一主图：状态主图
   ├─ nodes：客服流程节点
   ├─ requirements：节点下的履约小任务
   ├─ evidence_groups：每个小任务对应的证据组
   ├─ activation：主线、可选、用户触发、条件触发
   ├─ edges：节点之间的先后、依赖和转场
   └─ relation_groups：多节点组关系，如 all_of / any_of / ordered
└─ 二副表之一：知识副表 knowledge_table
   ├─ claim：需要保持正确的业务事实
   ├─ support：支持该事实的表达
   └─ refute：与该事实冲突的表达
└─ 二副表之二：限制副表 constraint_table
   ├─ trigger：何时触发限制
   ├─ safe_context：怎样说是安全处理
   ├─ prohibited：禁止动作或越界承诺
   └─ violation_scope：受保护对象、禁止动作和暧昧区
└─ 终止策略 terminal_policies
   └─ 用户忙碌、拒绝、开车等异常场景下，哪些后续流程应当被压制或终止
```

这四层的职责并不相同。

| 层次 | 负责什么 | 不负责什么 |
| --- | --- | --- |
| 状态主图 | 评估客服有没有完成流程、是否满足顺序、条件分支是否被正确触发。 | 不直接判断所有事实真伪，也不承担全部合规边界判断。 |
| 知识副表 | 独立核验客服有没有把业务事实说反、说错或混淆。 | 不推动流程进度，也不替代节点完成度。 |
| 限制副表 | 独立核验客服是否越界承诺、违规施压、在不该继续时继续介绍。 | 不要求客服必须主动讲某个流程点。 |
| 终止策略 | 处理忙碌、拒绝、开车等会改变流程适用性的上下文。 | 不把正确收束误判成主线缺失。 |

因此，文档中说“状态图”时，首先指的是主图；知识副表和限制副表是围绕主图工作的两个副表。它们和主图协同，但不应被理解成与主图节点并列的一组流程节点。这样的设计使系统能分别回答三个问题：流程是否完整、事实是否正确、边界是否合规。

这一步利用了大模型对复杂指令的理解能力，但不会让大模型逐条评估全部对话。

### 2.2 `longcat_client.py` 的小机制

`LongCatClient` 负责连接 LongCat API。项目文档和 example 按 `LongCat-Flash-Lite` 作为离线建图和二次仲裁模型说明。

关键机制包括：

1. **模型选择**：默认目标模型为 `LongCat-Flash-Lite`，便于用较低成本完成建图和仲裁。
2. **JSON 稳定抽取**：`extract_json_object()` 会从模型输出中提取 JSON，处理代码块包裹、尾逗号、字段缺逗号等常见格式问题。
3. **token 统计**：每次模型调用都会记录 prompt、completion 和总 token，最后写入 `run_token_usage.json`。
4. **建图耗时记录**：离线建图、二次补图分别计时，写入 `run_timing_summary.json`。
5. **模拟回放接口**：调试时可通过本地目录回放模型返回，但正式路径仍然是 LongCat API。

## 3. 第二步：理解 graph 的结构特征

SCEG 的 graph 不是一组关键词，也不是一段评分 prompt，而是一份可以被本地程序执行的评估标准。它的核心结构仍然是“一主图、二副表”：主图解决流程和结构问题，知识副表解决事实正确性问题，限制副表解决合规边界问题。

### 3.1 主状态图：先把客服流程变成可追踪坐标

一个节点通常对应客服必须完成的一个业务动作，例如：

| 业务域 | 节点示例 | 节点意义 |
| --- | --- | --- |
| 商家 | 身份确认 | 先确认对方是否为机构或校区负责人。 |
| 商家 | 传达升级内容 | 说明课程发布页会新增标准直播和低延迟直播两个选项。 |
| 商家 | 检查学员端费用 | 提醒商家确认低延迟直播是否适用已设置的学员端费用。 |
| 骑手 | 合同生效通知 | 告知今天飞毛腿合同已经签署并生效。 |
| 骑手 | 排名规则说明 | 说明报名和名额按系统排名规则走，站长不能人工干预。 |
| 骑手 | 退出流程 FAQ | 用户问退出时，说明 App 路径、前一天 Z 点前取消、次日生效。 |

节点不是简单关键词命中。每个节点下会继续拆成 requirement，每个 requirement 再绑定 evidence group。这样系统能判断“该动作完成到了什么程度”，而不是只看某个词有没有出现。

### 3.2 requirement：节点内部的最小履约单元

例如商家任务里的“传达升级内容”节点，并不只是要求说“升级”二字，而是包含多个小任务：

```text
inform_upgrade
├─ upgrade：说明发布页会新增标准直播和低延迟直播独立选项
├─ two_options：说明两种直播的区别和适用场景
└─ choose_by_course：说明后续可以根据课程类型自行选择
```

如果客服只说“页面会升级”，但没有说明两个选项或适用场景，节点就可能只是部分完成。这使评分具备细粒度解释能力。

### 3.3 evidence group：图里真正可执行的证据条件

每个 requirement 会配置一个或多个证据组。证据组不是写死在 evaluator 中，而是 LongCat 根据复杂指令生成，再由 compiler 规范化。

常见字段包括：

| 字段 | 含义 |
| --- | --- |
| `speaker` | 要匹配客服、用户，还是双方均可。 |
| `all` | 当前句必须同时包含的证据片段。 |
| `any` | 当前句命中任一片段即可。 |
| `min_any_hits` | 至少命中多少个 any 片段，防止一个宽泛词误判完成。 |
| `none` | 出现这些内容时排除命中。 |
| `regex_any` | 正则表达式证据。 |
| `number` | 数字、范围或单位匹配。 |
| `window_union` | 允许同一说话人的多句话合并满足证据组。 |
| `cross_turn` | 用于“客服询问、用户短答确认”这类跨轮证据。 |

这也是“基于证据的节点激活”的核心：节点是否完成，必须回到对话原话和证据组，而不是靠全局语义相似。

### 3.4 activation：图可以区分主线、可选分支和条件触发

真实客服任务不是所有节点都必须在每条对话里出现。SCEG 用 activation 表达节点何时需要被评估。

| activation | 含义 |
| --- | --- |
| `always` | 主线节点，默认需要评估。 |
| `optional` | 可选节点，默认不强制扣分。 |
| `user_triggered` | 用户提问或表达某需求后才激活。 |
| `condition` | 满足忙碌、开车、拒绝、无法配送等条件后才激活。 |

例如“退出飞毛腿 FAQ”只有骑手询问退出时才激活；“第三方系统开通引导”只有商家提到第三方系统或系统未显示时才激活；“开车终止策略”只有用户说正在开车时才激活。

### 3.5 edges 与 relation groups：图会评价顺序和结构

状态图中不仅有节点，还有节点之间的关系。

| 关系 | 用途 |
| --- | --- |
| strict order | 前置节点必须先完成，例如身份确认应在正式通知前。 |
| soft order | 建议顺序，错位会轻扣分但不一定硬失败。 |
| dependency | 后续节点依赖前置节点或用户触发。 |
| all_of | 一组节点都应完成。 |
| any_of | 一组节点中完成任意一个即可。 |
| ordered group | 一组节点需要大体按顺序出现。 |

因此系统不是只数“说了几个点”，还会判断“是否按合理客服流程推进”。

### 3.6 知识副表：事实判断从主流程中独立出来

复杂指令中有很多“不能说错”的知识，例如低延迟直播适用场景、第三方系统开通方式、飞毛腿单日/多日合同规则、退出生效时间等。这些内容不塞进主图节点里，而是放在 `knowledge_table` 中，由 `KnowledgeJudge` 单独核验。

知识副表的优势是：即使客服完成了主图流程节点，只要关键事实说反了，仍然会被识别为知识错误；反过来，客服没有主动讲某个未触发 FAQ 时，也不会因为知识副表存在该知识点就被误判为流程缺失。

### 3.7 限制副表：合规边界从流程中独立出来

客服不能越界承诺，不能在用户开车时继续讲业务，不能保证名额，不能私下承诺补偿。这些内容也不作为普通主图节点处理，而是放在 `constraint_table` 中，由 `ConstraintJudge` 单独核验。

限制副表的意义是把“不能做什么”从“应该完成什么”里分离出来：主图鼓励客服完成必要流程，限制副表防止客服为了完成流程而越界推进。限制副表通常包含：

```text
trigger：什么时候触发限制
safe_context：怎样说是安全的
prohibited：哪些动作或承诺禁止出现
unresolved：哪些暧昧表达需要仲裁
violation_scope：受保护对象、禁止动作、安全动作、暧昧区
```

### 3.8 terminal policies：异常分支会压制后续节点

用户说忙、拒绝、开车、不方便时，客服不一定还应该继续完成全部主线。terminal policy 会告诉 evaluator：如果客服已经按要求安全收束，后续节点可以被 suppress 为“不适用”。

这能避免把“客服正确结束通话”误判为“主线没有说完”。

## 4. 第三步：schema_repair_audit 判断是否需要二次补图

对应模块：

```text
schema_repair_audit.py
prompts/schema_graph_repair_prompt.md
```

第一次 LongCat 建图后，系统不会立刻拿图去评估，而是先做 schema gap audit。它检查的是结构缺口，不是本地补业务答案。

主要检查包括：

1. 节点是否缺失；
2. 数据集中的目标节点或目标 ID 是否无法绑定到 graph；
3. 知识副表是否缺 support/refute；
4. 限制副表是否缺 `violation_scope`；
5. terminal policy 的触发覆盖是否过窄；
6. FAQ、条件分支是否被误放进主线。

如果发现硬缺口，系统会把原始复杂指令、当前 schema、审计结果和绑定提示交给 LongCat-Flash-Lite 进行二次 repair。注意：本地只指出“哪里不完整”，不自己创造业务事实。

## 5. 第四步：Schema Linter 做结构规范检查

对应模块：

```text
schema_linter.py
```

Schema Linter 的目标是保证 LongCat 输出的图能稳定被本地 evaluator 执行。

主要小机制包括：

1. **节点检查**：检查 requirement 是否缺 evidence group，条件分支是否污染主线，过软要求是否不应刚性扣分。
2. **证据组检查**：移除重复 pattern，提示过宽或过窄的证据组，避免一个主题词直接完成节点。
3. **知识副表检查**：检查 support/refute 是否重叠，是否缺对象锚点，是否存在答案字段污染。
4. **限制副表检查**：检查 trigger、safe_context、prohibited、violation_scope 是否完整。
5. **关系检查**：检查主流程、分支节点、all_of/any_of/ordered 组是否能被解释。
6. **反泄漏检查**：防止 `wrong_statement`、`evidence_span` 进入证据组、知识 refute 或限制 prohibited。

Linter 的输出会写入 schema metadata 或 `schema_linter_report.json`，用于调试和展示。

## 6. 第五步：Schema Compiler 编译可执行规则

对应模块：

```text
schema_compiler.py
schema.py
```

Schema Compiler 把自然语言 schema 转换成 evaluator 可以直接执行的结构对象。

主要小机制包括：

1. **节点规范化**：统一节点 ID、名称、activation、requirements 和 evidence groups。
2. **证据组加固**：对大 any-list 增加 `min_any_hits` 或 window union，避免单个宽泛词误判完成。
3. **知识副表规范化**：整理 claim、support/refute、claim aliases 和对象锚点。
4. **限制副表规范化**：把 trigger、safe_context、prohibited、violation_scope 编译成统一结构。
5. **ID / alias 绑定**：把数据集中的 `target_node_id`、`target_id` 作为结构别名绑定到图，解决 LongCat 每次生成 ID 不完全一致的问题。
6. **旧格式兼容**：兼容早期图和最新 schema 格式。

Compiler 可以做结构标准化和别名绑定，但不能把负包的 `wrong_statement` 或 `evidence_span` 直接编译成判分答案。

## 7. 第六步：Dialogue Loader 读取对话

对应模块：

```text
dialogue_loader.py
```

`load_dialogues()` 负责读取 `data/dialogues` 中的 JSON。它支持两种格式：

- 新格式：`turns`；
- 旧格式：`dialogue`。

加载后统一为：

```text
id
sample_type：positive / negative
domain：merchant / rider
turns：多轮对话
metadata：场景、用户风格、预设错误或覆盖目标
```

它只负责加载和标准化，不做任何判分。

## 8. 第七步：domain compatibility filter 匹配业务域

对应模块：

```text
demo_runner._domain_compatibility_filter()
```

当 `data/dialogues` 同时包含商家和骑手对话时，系统不能把商家图套到骑手对话上。domain filter 会先读取 graph 的 `metadata.domain`，再读取 dialogue 的 `domain`。如果二者明确一致，则保留；如果不明确，则用 graph 文本和 dialogue 文本的字符 n-gram 兼容度做保守判断。

这不是业务硬编码，因为用于判断的文本来自当前 graph 和当前 dialogue，而不是代码里写死的商家/骑手词典。

## 9. 第八步：Evidence Extractor 抽取证据单元

对应模块：

```text
evidence_extractor.py
evidence_units.py
normalizer.py
```

`EvidenceExtractor` 把每轮对话转成 `EvidenceUnit`。每个证据单元包含：

```text
turn_index：轮次
speaker：客服或用户
text：原始文本
normalized_text：规范化文本
numbers：显式数字、范围、单位
```

它不会判断业务含义，只做格式化和可匹配化。比如“5到10秒”“1-2天”“前一天 Z 点前”会被保留为数字、范围或时间线索，供后续 matcher 和 judge 使用。

## 10. 第九步：Evidence Matcher 匹配节点证据

对应模块：

```text
evidence_matcher.py
```

`EvidenceMatcher` 根据 graph 中的 evidence group 去匹配对话证据。

主要小机制包括：

1. **单句匹配**：根据 all、any、none、regex、number 等字段匹配当前句。
2. **多句窗口合并**：同一客服多句话可以共同满足一个 requirement，适应真实话术拆句。
3. **跨轮确认**：支持“客服询问 + 用户短答确认”这类证据。
4. **宽证据组降噪**：大 any-list 默认要求多个命中，并对重复出现的宽泛主题词降权。
5. **说话人约束**：身份确认、说明规则、用户触发等需要区分 assistant 和 user。
6. **通用中文容错**：允许“知道吗 / 了解吗 / 知情吗”“不方便 / 忙 / 稍后”等通用表达变体。

这一层只执行 graph 给出的证据条件，不内置商家或骑手的具体任务答案。

## 11. 第十步：Knowledge Judge 核验知识事实

对应模块：

```text
knowledge_judge.py
```

`KnowledgeJudge` 处理 `knowledge_table`，输出三态：

| verdict | 含义 |
| --- | --- |
| 支持 | 对话说法与知识副表一致。 |
| 冲突 | 对话说法与知识副表相反。 |
| 证据不足 | 对话触及该主题，但本地证据不足以稳定判断。 |

主要小机制包括：

1. **claim evidence**：每个知识项拆成 claim，每个 claim 有 support/refute patterns。
2. **对象锚点**：从知识 ID、名称、alias、claim patterns 中提取锚点，避免兄弟知识互相误伤。
3. **方向极性保护**：识别高/低、前/后、可/不可、已/未等方向反转。
4. **数字和范围判断**：比较数字、范围、时间和单位是否与 claim 一致。
5. **占位符变量保护**：支持 X 单、Y 天、W 天等匿名变量，不把它们误当具体业务硬编码。

## 12. 第十一步：Constraint Judge 核验限制边界

对应模块：

```text
constraint_judge.py
generic_customer_service_expressions.py
```

`ConstraintJudge` 处理 `constraint_table`，输出：

| verdict | 含义 |
| --- | --- |
| 安全 | 命中安全处理方式。 |
| 违规 | 命中禁止动作或越界承诺。 |
| 证据不足 | 命中暧昧区，需要后续仲裁。 |

限制判断的重点是区分“说明边界”和“越界承诺”。例如：

```text
不能承诺给您优惠，以页面显示为准      → 安全边界说明
我帮您申请优惠券，肯定能减免费用      → 可能违规
```

本地代码可以内置“不能、无法、不承诺、保证、确保、一定、帮您、必须、否则”等通用话语行为算子；但“优惠券、名额、派单、退出生效”等具体对象必须来自 graph 的 protected object 或 prohibited pattern。

## 13. 第十二步：Graph Evaluator 综合节点、关系、知识、限制和分数

对应模块：

```text
graph_evaluator.py
```

`GraphEvaluator` 是本地评估主干，但它不是在知识和限制之后再额外做一次附加检查，而是在节点证据、知识事实、限制边界都已经形成事件结果后，把这些结果统一纳入节点状态、关系结构、上下文转场和总分计算。

也就是说，知识副表和限制副表的核验发生在综合评分之前；Graph Evaluator 负责把三条证据线合并成最终的可解释评估结论。它会生成四类核心结果。

### 13.1 节点激活

先根据 activation 判断节点是否 active。未触发节点标记为“不适用”，不作为缺失扣分。

例如用户没有问退出飞毛腿，就不强制评估退出 FAQ；用户说正在开车，则触发开车终止策略。

### 13.2 requirement 与 node 评分

评分顺序是：

```text
evidence group 是否命中
→ requirement 是否命中
→ node 分数
→ node 状态：已完成 / 部分完成 / 缺失 / 不适用
```

默认阈值来自 `config/default_runtime.json`。节点分不会只看关键词，而是看 requirement 下的证据组是否满足。

### 13.3 结构关系评分

`_relation_score()` 会检查：

- 前置节点是否缺失；
- 后续节点是否缺失；
- 后置节点是否早于前置节点出现；
- relation group 是否满足 all_of、any_of、ordered、min_completed 等要求。

因此系统可以识别“内容都说了，但顺序明显不合理”的情况。

### 13.4 上下文转场

`_resolve_context()` 处理 terminal policies。例如：

- 用户开车：客服应礼貌结束，不能继续主动讲业务；
- 用户忙：客服应简短说明或约后续联系；
- 用户坚持拒绝配送：客服应安慰后收束，不能继续施压。

如果客服正确处理终止策略，后续主线节点会被压制为“不适用”。如果客服继续推进，则可能触发限制或仲裁候选。

### 13.5 四维总分和总体验收上限机制

基础总分由四维组成：

| 维度 | 默认权重 | 含义 |
| --- | ---: | --- |
| 节点完成度 | 0.50 | 必需节点和 requirement 是否完成。 |
| 结构关系 | 0.15 | 节点顺序、依赖和关系组是否合理。 |
| 知识正确性 | 0.20 | 是否说错知识副表事实。 |
| 限制合规性 | 0.15 | 是否出现越界承诺、违规施压或不当继续通话。 |

总体验收上限机制用于处理门槛型错误。比如核心流程缺失、知识冲突、限制违规被确认后，系统会根据错误严重程度限制该样本最终能够达到的最高分，避免客服因为说了很多无关或次要内容而把总分“平均拉高”。这是正包、负包和灰区样本都共享的总机制，不是负包专属规则；负包只是更常触发它，因为负包本来就是为了验证系统能否识别明确错误。

## 14. 第十三步：Dataset Interface 做正负包验收

对应模块：

```text
dataset_interface.py
```

GraphEvaluator 评价的是“客服表现”；DatasetInterface 判断的是“这条样本作为正包或负包是否通过验收”。

### 14.1 正包验收

正包通过通常要求：

- 总分达到正包阈值；
- 没有知识冲突；
- 没有限制违规；
- 没有 active 必需节点缺失；
- 没有未处理的上下文转场问题。

如果正包是场景型样本并带有 `coverage_targets`，它不一定要求主线全部出现；只要覆盖目标分支且没有硬错误，也可以通过。

### 14.2 负包验收

负包带有 `injected_errors`，用于说明预设错误目标。系统会检查预设错误是否被 evaluator 识别。

常见错误族包括：

| error_family | 本地判定来源 |
| --- | --- |
| `flow_missing` | 目标节点或 requirement 缺失。 |
| `knowledge_violation` | 知识副表产生冲突事件。 |
| `constraint_violation` | 限制副表产生违规事件。 |

重要红线：`wrong_statement` 和 `evidence_span` 不能被直接编译成规则。负包通过必须来自节点缺失、知识冲突、限制违规或经过灰区仲裁确认的问题，而不是读答案。

## 15. 第十四步：Score Adjuster 同步分数和验收

对应模块：

```text
score_adjuster.py
```

`apply_dataset_score_adjustments()` 负责把验收结论和总体验收上限机制对齐。

它解决的问题是：如果某个门槛型错误已经被确认，最终报告不能仍然表现为“高分无事发生”。这里的总体验收上限机制是全局评分协议，不是负包专属机制。正包中如果出现严重知识冲突或限制违规，也会被同样压低；负包只是通常带有预设错误，因此更容易在案例讲解中体现这一机制。典型处理包括：

- 流程缺失确认后，按照流程错误严重度设置总分上限；
- 知识错误确认后，按照事实冲突严重度设置总分上限；
- 限制违规确认后，按照合规风险严重度设置总分上限；
- 正包场景目标满足且无硬错时同步通过状态。

这属于统一评测协议，不是针对某个业务场景的硬编码。

## 16. 第十五步：Oracle Router 生成灰区候选

对应模块：

```text
oracle_router.py
```

不是所有表达都适合本地规则直接判死。`OracleRouter` 会生成局部灰区候选，但它不直接调用 LLM。

候选来源包括：

1. requirement 覆盖灰区；
2. 知识核验灰区；
3. 限制边界灰区；
4. 上下文转场灰区；
5. 负包样本验收灰区。

每个候选都必须包含 schema 锚点和对话证据，例如 node_id、requirement_id、knowledge_id、constraint_id、question、evidence、need、strength 等。没有锚点、没有证据的候选不会进入后续仲裁。

## 17. 第十六步：Local Second Filter 本地二筛

对应模块：

```text
local_second_filter.py
```

本地二筛是工业可行性的关键。它在真正调用 LongCat 前先过滤候选。

主要小机制包括：

1. **schema anchor score**：候选是否绑定明确节点、知识或限制。
2. **evidence anchor score**：候选是否有具体对话原话。
3. **local strict promotion**：本地已经能确认的问题，不再送 LLM。
4. **same evidence ledger relation**：如果同一证据已经在账本中明确支持或安全，不重复送审。
5. **重复候选合并**：同类同证据候选合并，只发送代表项。
6. **低价值候选忽略**：无锚点、空证据或弱证据候选不送审。

这一步避免了把所有样本交给大模型，也让 LongCat 的调用更少、更集中。

## 18. 第十七步：LongCat-Flash-Lite 二级仲裁

对应模块：

```text
llm_verifier.py
```

二级仲裁只处理本地二筛后留下的少量候选。它不是让 LongCat 给整段对话总评，而是要求 LongCat 判断一个局部问题：

```text
这个 schema 锚点下，这几句客服原话是否构成问题？
```

返回结果只有三类：

| 结果 | 含义 |
| --- | --- |
| `confirmed_issue` | 问题成立。 |
| `rejected_issue` | 问题不成立。 |
| `uncertain` | 证据不足。 |

仲裁 payload 只包含候选问题、schema 绑定、evaluator 账本、客服实际表达摘要和验收上下文。它不会把 `wrong_statement` 当作标准答案直接喂给 LongCat。

三种运行模式为：

| 模式 | 作用 |
| --- | --- |
| `off` | 完全关闭，不调用 LongCat。 |
| `shadow` / 审计模式 | 记录 LongCat 判断，但不改最终结果。 |
| `assist` / 辅助模式 | 对负包灰区问题，LongCat 或本地二筛确认后可改为“仲裁通过”。 |

## 19. 第十八步：Report Explainer 与 HTML 报告

对应模块：

```text
report_explainer.py
report_html.py
```

报告模块把结构化评估结果翻译成评委能读懂的中文。

输出内容包括：

- 一句话结论；
- 总分和四维分数；
- 正负包验收结果；
- 节点和 requirement 命中情况；
- 证据组期望与命中原话；
- 知识支持、冲突、证据不足；
- 限制安全、违规、证据不足；
- 结构关系事件；
- 上下文转场事件；
- 本地二筛和 LongCat 仲裁结论；
- token 使用和运行时间；
- schema linter 提示。

报告分为简版和详版：

| 报告 | 面向对象 |
| --- | --- |
| `report_simple.html` | 非技术评委，重点看结果、通过率和失分归因。 |
| `report_detail.html` | 技术评委，重点看节点、证据、知识、限制和仲裁明细。 |
| `case_reports/*.html` | 单条样本的完整追踪。 |

## 20. 最后一步：demo_runner 汇总运行产物与演示入口

对应模块：

```text
demo_runner.py
app.py
app_offline.py
scripts/run_offline_graph.py
```

`demo_runner.py` 是调度器，负责把前面的模块串起来。它在流程最后汇总产物，而不是方法中途突然引入 demo。

### 20.1 在线建图入口：`app.py`

`app.py` 适合展示完整链路：

```text
输入复杂指令
→ LongCat-Flash-Lite 建图
→ 必要时二次补图
→ 本地评估对话
→ 可选二级仲裁
→ 生成报告
```

### 20.2 离线图评估入口：`app_offline.py`

`app_offline.py` 适合答辩和复现实验：

```text
读取已有 graph.json
→ 本地编译和检查
→ 读取 dialogues
→ 本地评估
→ 可选二级仲裁
→ 生成报告
```

它避免每次展示都重新调用 LongCat 建图，结果更稳定，也更适合现场演示。

### 20.3 命令行入口：`scripts/run_offline_graph.py`

命令行适合快速验收：

```bash
PYTHONPATH=src python scripts/run_offline_graph.py \
  --graph runs/graphs_offline/course_publish_upgrade_v1.json \
  --dialogues data/dialogues \
  --pack all \
  --llm-mode off
```

## 21. 交付检查工具

对应目录：

```text
tools/
├─ hardcode_guard.py
├─ anti_leak_guard.py
└─ negative_purity_check.py
```

推荐交付前运行：

```bash
PYTHONPATH=src python tools/hardcode_guard.py
PYTHONPATH=src python tools/anti_leak_guard.py
PYTHONPATH=src python tools/negative_purity_check.py data/dialogues/negative_pack
```

它们分别检查：

| 工具 | 作用 |
| --- | --- |
| `hardcode_guard.py` | 检查核心 evaluator 是否混入商家、骑手等业务硬编码。 |
| `anti_leak_guard.py` | 检查负包答案字段是否被编译进判分逻辑。 |
| `negative_purity_check.py` | 检查负包是否有明确、可验收的预设错误。 |

## 22. 为什么这个方法工业可行

### 22.1 高成本理解前置到离线建图

复杂客服指令数量通常远少于待评估对话。SCEG 把最需要大模型理解能力的部分放到离线阶段：

```text
复杂指令 → LongCat-Flash-Lite → 状态主图 / 知识副表 / 限制副表
```

这份 graph 可以缓存、复用、复查，不需要每条对话都重新让大模型理解复杂指令。

### 22.2 大规模样本由本地 evaluator 执行

正式评估中，证据抽取、节点命中、关系评分、知识判断、限制判断、正负包验收和报告生成都在本地完成。它具备：

- 成本低；
- 速度快；
- 可复现；
- 可批量回归；
- 可输出证据链和中文解释。

### 22.3 只有少量灰区送 LongCat

系统先生成灰区候选，再用本地二筛过滤、合并和降噪，只把有 schema 锚点、有对话证据、语义方向不稳定的少量候选送 LongCat-Flash-Lite。

这使 LongCat 更像“局部仲裁员”，而不是“全局黑盒裁判”。

### 22.4 产物能直接展示成本和证据

系统会输出：

- `run_token_usage.json`：LongCat 调用次数和 token；
- `run_timing_summary.json`：建图、评估、报告生成耗时；
- `llm_verifier_summary.json`：二级仲裁数量和结果；
- `all_reports_merged.json`：所有样本结构化结果；
- HTML 报告：中文解释和证据追踪。

因此工业可行性不是口头承诺，而是可以在运行产物中直接检查。

## 23. 方法优势总结

SCEG 的优势可以概括为四点：

1. **结构化**：复杂指令被拆成状态主图、知识副表、限制副表和终止策略；
2. **可解释**：每个结论都能追溯到节点、requirement、证据组和原话；
3. **低成本**：LongCat 主要用于离线建图和少量灰区仲裁，大规模评估由本地执行；
4. **可落地**：适合真实客服质检、复杂任务验收和批量回归测试。
