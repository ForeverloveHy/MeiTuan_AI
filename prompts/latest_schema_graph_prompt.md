# SCEG 五步建图契约总提示词

当前项目不再使用旧式“一次性生成整张 schema”的提示词。LLM 必须按五步契约生成：主状态图、知识表、限制表、一级元素、二级元素扩张。本文件是入口说明；实际运行时，系统会先注入 `sceg_method_memory_prompt.md`，再注入对应阶段提示词。

## 为什么必须分五步

复杂指令里同时存在四类语义：客服动作、事实真假、禁止边界、整体质量。它们不能混在同一个 prompt 中一次生成，否则会出现节点 fact 化、知识表礼貌化、限制表百科化、soft 混入 hard、element 粘连整句等问题。五步契约的核心是：每一步只解决一种对象，并且输出字段就是本地 evaluator 的接口。

## 五步任务与内涵

### 第一步：主状态图 `schema_core_graph_prompt.md`

只回答“客服应该怎样推进对话”。输出 `graph_id`、`name`、`metadata`、`nodes`、`edges`、`relation_groups`、`terminal_policies`。

主图负责：开场、身份确认、主线通知、必要询问、用户状态分支、FAQ 追问、越界处理、终止与回流。主图不负责：事实值真假、禁止承诺、语气质量、元素池扩张。

### 第二步：知识表 `schema_knowledge_table_prompt.md`

只回答“客服说出的事实是否正确”。输出 `knowledge_table`。

知识表负责：对象、属性、正确值、数值、时间、金额、次数、比例、区间、条件结果、适用范围、影响方向。知识表不负责：客服是否说了这一步、是否礼貌、是否违规承诺。

### 第三步：限制表 `schema_constraint_tables_prompt.md`

只回答“客服有没有碰到负向对象或整体质量问题”。输出 `hard_constraint_table` 与 `soft_constraint_table`。

hard 是负向对象精表，必须有受限对象 + 违规动作 + 安全翻转。soft 是整体质量表，只表达自然度、长度、重复、清晰度、互动机会等质量维度。hard 通常 3-8 类，最多 10 类；soft 通常 3-6 类。

### 第四步：一级元素 `schema_atom_element_refinement_prompt.md`

只回答“每个 atom 应该拆成哪些短语槽”。输入是 `atom_transport`，输出 `element_refinements`。

一级元素负责把 atom_text 拆成可匹配短语，并标注 main/fact。它不改 atom，不改节点，不改知识事实，不扩 pool。节点 atom 不得出现 fact=true；知识 correct 必须有对象 main + 正确 fact；hard negative 必须有对象 main + 违规动作。

### 第五步：二级元素扩张 `schema_element_expansion_prompt.md`

回答两类问题：客服侧“已有 element.value 有哪些等价说法”；用户触发侧“真实用户可能怎么说，以及每句话应拆成哪一组 trigger elements”。输入是带已有 element 的 `atom_transport`，输出 `secondary_expansions`。

客服侧只填 pool，不新增客服 element，不删除 element，不修改 value/main/fact，不改变事实、极性、数值、时间、金额、次数、比例、区间、条件、步骤或结论。用户 trigger 侧可以新增带 `source_text` 的 trigger_groups，但只能表达原触发条件的同义用户话术，不能新增事实或新意图。

## 补充子任务

主图补充、知识补表、限制补表都不是新阶段，而是阶段内修正。补充子任务现在采用“缺口诊断 + 本地通用词典提示 + 严格回写”的机制：

1. 主图补充会收到 `local_supplement_hints`。它只来自通用客服动作族和结构审计，用来提示可能漏掉的开场、询问、条件分支、追问、终止抑制、操作引导、收尾等主图功能。它不是业务答案库。主图补充必须输出完整修正后的 `graph_core`，只补原指令支持的主图动作和关系。
2. 知识补表会收到通用事实槽提示，只提醒数值、时间、范围、入口、路径、条件结果等形状。事实值仍必须来自原指令。知识补表输出完整修正后的 `knowledge_table`，只补明显事实遗漏和结构错误。
3. 限制补表会收到通用边界与质量提示，只提醒禁止承诺、代操作、越权、安全停止、软质量等形状。限制补表只能 patch-only：输出 `add_hard_constraint_table`、`add_soft_constraint_table`、`remove_constraint_ids`，最多补 0-3 条 hard，不得完整重写，不得爆炸扩写。
4. 本地词典只负责“提醒模型检查哪类缺口”，不负责“替模型生成任务事实”。若原指令没有明确依据，任何提示都不能被采纳。

## 字段语义总原则

1. 字段不是装饰，是本地 evaluator 的执行接口。
2. `group` 内部是 AND，多个 group 是 OR。
3. `main=true` 是召回主干，不是“重要词”。
4. `fact=true` 是精判槽，必须同组绑定非 fact 的 main。
5. `pool` 只放等价表达，不能发明新事实。
6. 主图只履约，知识只验事实，限制只查负向对象或软质量。
7. 禁止回到旧 `requirements/evidence_groups` 路线；`requirements` 只保留空数组兼容。
8. 对外提示词只讲 atom，不讲 anchor；内部兼容字段不应影响模型理解。

## 本轮补充：字段含义必须解释到执行层
生成任何阶段输出时，不允许只“知道字段名”。每个字段都要能回答三个问题：
1. 这个字段被本地 evaluator 用来做什么？
2. 它和相邻字段的边界是什么？
3. 如果写错，会造成漏检、误杀、还是结构不可编译？

高风险字段的统一口径：
- `name` 和 `description` 主要供报告展示，不是严格匹配依据；关键判断必须进入 `atom.text` 或对应 group。
- `required` 表示触发条件成立后是否必须履约，不等于“重要”。
- `severity/weight` 表示错误影响程度，不决定是否生成该项。
- `main` 是召回主干，不是“重要词”；`fact` 是精判槽，不是“事实句”。
- `trigger_groups` 是用户状态或触发条件，不是客服话术。
- `selector_groups` 只找对象/属性，`correct_groups` 才判断正确事实。
- `negative_groups` 查违规表达，`safe_groups` 查同一对象上的安全翻转。
- `local_supplement_hints` 只是通用缺口提示，不是业务事实，不得机械复制成节点或表项。


## 限制表补表强制决策补充
当限制表补表阶段收到 required=true 的硬边界信号时，不能空返回。必须先输出 hard_candidate_decisions；每个 signal 只能 convert_to_hard、already_covered 或 reject_as_not_hard。convert_to_hard 必须对应 add_hard_constraint_table 中的正式 hard 项，reject_as_not_hard 必须说明为什么只是知识事实、软质量或证据不足。


二十一、角色感知 element 派生原则
1. element 不应只从任务语义标签里机械切词，而应从“最可能出现的对话话语”里派生。
2. 客服侧话语由同一系统生成，表达趋同。因此 node_atom、knowledge、hard safe/negative、soft 的 element 应先构造最可能客服答话，再从该答话拆对象、动作、属性、事实值、违规动作或安全翻转。
3. 用户侧话语开放且不可控。因此 activation trigger 不能只按 trigger_hint 一句话切词，必须先生成大量可能用户说法，再从这些说法中抽共同触发槽，并把其它同意图说法放进 pool。
4. 对知识事实，selector/correct 不应从抽象事实标题拆，而应从客服最可能正确回答拆。selector 找对象与属性，correct 复用对象属性并加入正确 fact。
5. 对 hard 限制，negative 来自可能违规客服说法，safe 来自期望安全客服说法；二者必须围绕同一受限对象，pool 不得互相混入。
6. 这个原则的目标是让 element 接近真实评估文本，而不是接近 schema 编写者的抽象任务标题。


【element 生成与扩张的最新契约】
- 第四步只做一级元素：客服侧从最可能客服答话拆 element；用户 trigger 只保留最小触发种子。
- 第五步分两类扩张：客服侧对已有 element.value 扩 pool；用户侧先扩 likely_user_texts，再将每条用户话术转为一个 trigger_groups 组。
- 用户 trigger 多个 group 是 OR，任一 group 命中即可触发；每个 group 内部是 AND。

【当前补图优先级】
当生成结果与理想图严重不对齐时，优先把问题修进二次补图或二次补表，而不是靠报告解释：
1. FAQ 合并过度：二次补图必须按用户问题对象拆成多个 faq 节点。
2. FAQ 事实进入主线：二次补图必须把只在追问时回答的事实移出 required main。
3. 条件节点误入 required sequential：二次补图必须修 relation_groups，避免未触发分支被当缺失。
4. 用户 trigger 覆盖不足：第五步必须先扩 likely_user_texts，再逐条 element 化为 OR trigger group。
5. hard 重复或过泛：二次补表必须 already_covered/remove_constraint_ids，不新增重复 hard，不使用过宽对象替代具体对象。

【element 质量增强口径】
1. 客服侧 element 的来源是“系统最期望客服说出的自然答话”，不是节点名、知识标题或任务标签。
2. 用户侧 trigger 的来源是“多条真实用户可能说出的 source_text”，不是一个抽象 trigger_hint。每条 source_text 生成一个 OR trigger group。
3. trigger group 禁止靠“我/用户/用户/对话对象/客户/对方”触发；必须包含状态、意图、对象或极性。
4. 抽象 element 要重写：问题、情况、处理、规则、进行、相关、内容、信息、知识库、流程等不能作为唯一 main。
5. hard element 先具体化再扩张：具体对象优先，重复 hard 合并，过泛对象删除或收窄。
6. knowledge selector 必须能召回错误事实；correct/value_check/wrong_groups 才负责判断对错。


【最后一轮补图强约束：只做可执行修复，不写展示话】
1. 主线必达节点必须 activation.mode=always。若一个节点承载核心告知、核心说明、核心升级/变更内容，不得写成 user_triggered 或 condition。
2. condition、user_triggered、faq、out_of_scope、terminal 节点不得放入 required sequential；只能通过 condition_on、exclusive_branch、any_of、optional_after 或 terminal_after 连接。
3. 若用户状态是“忙/没空/不方便但允许简短沟通”，处理节点后必须回流主线；不得用 suppress_after 压制主线。只有明确终止状态才 suppress 主线。
4. 若用户状态是安全风险、无法沟通、坚持拒绝或明确终止，terminal 节点必须有 activation trigger_hint 和至少一组用户侧 trigger_groups，不能 optional 空触发。
5. 信息获取类 atom 不要只写“询问 X”。必须写成“确认或根据用户已提供信息获取 X”。如果用户已主动给出状态，客服可以直接使用，不必重复问。
6. FAQ 必须一问一类；一个 FAQ 节点只回答一个问题对象。若一个节点里混有多个互不等价的问题对象，必须拆节点。
7. out_of_scope trigger 不得用“其他问题/问题/情况/事情”作为 main。必须是明确职责、权限、边界、不可确认、需转相关人员等状态。
