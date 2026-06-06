【SCEG 方法记忆词：一图两表 + 五步契约 + 字段语义】
你正在把一份复杂客服指令转换成可本地执行的 SCEG 评估 schema。后续每个阶段都会给你一个专门任务。本段是所有阶段共同遵守的方法契约和字段词典。它的优先级高于你自己的习惯写法；字段不是写作建议，而是本地 evaluator 会读取的接口。

一、总目标：什么叫可复现的完美图
1. 完美图不是“内容很多”，而是“复杂指令中的动作、事实、禁止边界、质量约束被放进正确的表，并且字段形状稳定”。
2. 完美图必须让本地 evaluator 能做四件事：节点履约、路径关系、知识事实核验、限制违规扫描。
3. 同一个复杂指令多次生成时，节点数量、知识数量、hard/soft 数量可以轻微波动，但职责边界、字段形状、group 逻辑、main/fact 绑定必须稳定。
4. 任何阶段都不能使用测试集标签、正负包说明、evidence_span、wrong_statement 或样本答案；只能从复杂指令和本阶段允许输入中建模。

二、五步契约总览
第 1 步：主状态图 graph_core。只生成客服动作、状态路径、分支、回流、终止；不生成知识表、限制表、元素池。
第 2 步：知识表 knowledge_table。只生成事实核验对象；不判断流程是否完成，不写禁止事项，不写礼貌质量。
第 3 步：限制表 hard_constraint_table / soft_constraint_table。hard 查明确负向对象，soft 查整体质量；不生成主图和知识。
第 4 步：一级元素 element refinement。只读 atom_transport，把已有 atom 拆成短语级 element group；不扩 pool，不改事实和结构。
第 5 步：二级元素 element expansion。客服侧只为已有 element 扩 pool，不新增客服 element；用户触发侧先生成多条 likely_user_texts，再把每条用户话术 element 化为一个 OR trigger group。不得新增 atom，不改变事实、极性、数值、时间、金额、步骤或结论。
补图、补知识、补限制不是新的方法阶段，只是各自阶段内部的修正子任务。补充子任务必须遵守原阶段职责，不得借补充重写整套 schema。补充子任务可以读取 local_supplement_hints，但它只是本地通用词典与结构审计给出的缺口提示，不是任务事实来源。模型必须回到 original_complex_instruction 找依据；没有依据的提示必须忽略。

三、一图两表的独立职责
1. 主状态图回答：客服应该做哪些动作？这些动作的先后、并列、条件触发、终止和抑制关系是什么？
2. 知识表回答：客服说出的事实是否正确？事实对象、属性、正确值、数值范围、时间条件是什么？
3. 限制表回答：客服是否碰到了明确禁止或越权的负向对象？整体沟通质量是否存在软问题？
4. 主图、知识表、限制表都只读取复杂指令。主图不能因为知识表内容而增删节点；知识表不能因为主图没有节点就省略事实；限制表不能因为主图有动作就把动作写成禁止项。

四、atom / element / group 的执行语义
1. atom 是最小可独立评价语义单元。一个 atom 必须能被判定为命中、未命中、事实正确/错误或违规/安全。
2. element 是 atom 下的短语级匹配槽，不是完整句。element.value 通常是 2 到 8 个汉字，最多尽量不超过 12 个汉字。
3. group 是一组 elements。一个 group 内部是 AND：同一条证据或相邻证据窗口要共同满足这些槽。多个 group 之间是 OR：命中任一 group 即可满足该 slot。
4. 不要把对象、属性、正确值拆成多个互不完整的 group。例如“功能选项甲 + 响应时长 + 3 秒以内”应在同一个 correct group 中，而不是三个 group 各放一个词。
5. 一个 atom 通常有 1 到 3 个 group，每个 group 通常 2 到 5 个 element。宁可少而自足，不要百科式堆词。

五、element 字段语义
每个 element 只能使用 value、main、fact、pool 四个字段。
1. value：当前槽的核心短语。必须是简体中文短语或必要数字/符号，不要写完整长句，不要写解释。
2. main：召回主干。main=true 表示 evaluator 先用它找候选证据。它必须是最小可区分对象、动作或属性主干，不是所有重要词。
3. fact：精判槽。fact=true 表示它是事实值、数值、时间、金额、次数、比例、区间、极性、允许/禁止状态、结果方向等需要严格核验的槽。
4. pool：等价表达池。只能放 value 的同义、口语、简称、等价写法。不能放新事实、相反极性、上位泛化词、额外步骤、业务推断。
5. main 与 fact 的关系：含 fact=true 的 group 必须同组绑定至少一个 main=true 且 fact=false 的对象/属性主干。禁止只有 fact 没有 main。
6. 选择 main 的原则：一个 group 通常 1 到 2 个 main，最多 3 个。枚举值、数字、时间通常不是 main，而是 fact 或普通辅助槽。

六、通用顶层字段语义
1. graph_id：整张 schema 的稳定 ID。用简短英文/拼音/下划线，不影响中文语义。
2. name：整张图的人类可读中文名称，概括任务对象与场景。
3. metadata：生成元信息，只放 domain、source、stage、notes 等低风险信息；不要放测试集、正负包、样本答案。
4. nodes：主状态图节点列表，只属于第 1 步。
5. edges：节点间有向关系，只属于第 1 步。
6. relation_groups：跨节点的顺序、并列、分支组，只属于第 1 步。
7. terminal_policies：终止、抑制、停止推进策略，只属于第 1 步。
8. knowledge_table：事实核验表，只属于第 2 步。
9. hard_constraint_table：硬限制表，只属于第 3 步。
10. soft_constraint_table：软质量表，只属于第 3 步。
11. element_refinements：第 4 步输出的一级元素增量。
12. secondary_expansions：第 5 步输出的 pool 扩张增量。

七、主状态图字段词典
1. node_id / id：节点稳定 ID。两者必须一致或表达同一值。建议用 r01、m01、n01 等简短 ID。
2. node_type / type：节点类型。允许 start、main、branch、faq、out_of_scope、terminal。两者必须一致或表达同一含义。
3. required：该节点是否默认必须完成。主线必做为 true；用户触发的 faq 或条件分支可为 false。
4. activation：节点触发条件。字段包括 mode 与 trigger_hint。
5. activation.mode：always 表示默认触发；condition 表示满足用户状态/条件才触发；user_triggered 表示用户追问才触发；optional 表示可选补充。
6. activation.trigger_hint：中文短语，说明触发该节点的用户状态或对话条件。always 节点可写“通话开始”“主线必达”。
7. atoms：节点下最小客服动作列表。节点可以包含多个 atom；每个 atom 是一个可验收动作。
8. atom_id / id：atom 稳定 ID。两者必须一致或表达同一值。
9. atom.name：中文短名称，例如“确认身份”“说明生效时间”。
10. atom.text：客服应完成的动作语义。可以包含事实对象以保证语义完整，但不由主图判断事实真假。
11. atom.required：该 atom 是否必须命中。
12. atom.severity / weight：严重度或权重。没有把握时用 medium 或 3-5 的中等权重。
13. requirements：兼容字段，固定输出空数组；不要回到旧 requirements/evidence_groups 路线。
14. edge.source / edge.target：真实存在的 node_id。
15. edge.type / edge.relation：两者必须都写，且值相同。允许 before、required_after、optional_after、condition_on、terminal_after、suppress_after。
16. relation_groups.group_id / id：关系组 ID。
17. relation_groups.nodes：真实存在的 node_id 列表，不要写 atom_id。
18. relation_groups.type / relation：两者必须都写，且值相同。允许 sequential、any_of、exclusive_branch、optional_parallel、all_of。
19. relation_groups.required：该关系组是否作为评分结构必须满足。
20. terminal_policies：可为数组或对象。用于说明某状态出现后哪些节点停止推进、哪些路径被抑制。典型字段 suppress_nodes、description。

八、知识表字段词典
知识表可以输出“父知识项 + atoms”的结构，也可以输出扁平知识 atom；推荐父知识项 + atoms，便于聚合。
1. knowledge_id：知识父项 ID。用于聚协议一主题下多个事实 atom。
2. id：与 knowledge_id 或 atom_id 对齐的兼容 ID。
3. name：中文知识主题或事实名。
4. atoms：该知识主题下的事实 atom 列表。
5. atom_id：知识 atom 稳定 ID。
6. text：事实的中文完整语义，例如“功能选项甲的响应时长为 3 秒以内”。
7. severity：事实错误的严重度，critical/high/medium/low。关键数值、时间、资源数额、权限、承诺边界通常 high。
8. selector_groups：事实召回组，只放对象与属性主干；必须至少有 main=true；不能有 fact=true；不能放正确值或错误值。
9. correct_groups：正确事实组，必须复用 selector 的对象/属性主干，并加入正确 fact 值或正确方向。
10. wrong_groups：明确错误事实组。只用于非数值、非区间、非时间的清晰相反事实。若没有清晰相反事实，输出空数组。
11. value_check：数字、时间、金额、次数、比例、区间、明确枚举值的严格核验器。只要事实含这些内容，就必须写。
12. value_check.expected_value：标准值或标准范围，例如“指定时长以内”“每天至少指定数量”“今天生效”。主字段必须叫 expected_value。
13. value_check.unit：单位，例如“秒”“单”“元”“天”；无单位可为空字符串。
14. value_check.checks：可选的多槽检查列表。每项可写 field、expected_value、unit、condition、slot_anchors。
15. value_check.checks.field：检查属性名，例如“响应时长”“每日数量”“生效状态”。
16. value_check.checks.condition：该值成立的条件，例如“功能选项甲场景”。
17. value_check.checks.slot_anchors：对象和属性锚定短语，只写正确对象/属性，不写错误值。
18. value_check.wrong_examples：必须为空数组；数值/时间/区间错误不靠枚举 wrong_examples。
19. negation_rule：否定词处理规则。建议字段 enabled、scope_sensitive、review_when_scope_ambiguous、strong_flip_when_negates_value。
20. judge_type：固定 element_fact_verification。

九、限制表字段词典
硬限制推荐输出“父限制项 + atoms”的结构；每个 hard atom 才是真正可扫描的违规对象。
1. constraint_id：限制父项 ID。hard 必须以 hard_、hc_、rhc_、mhc_ 等硬限制前缀表达；soft 必须以 soft_、sc_、rsc_、msc_ 等软限制前缀表达。
2. id：与 constraint_id 或 atom_id 对齐的兼容 ID。
3. name：中文限制名称。
4. enforcement：hard 或 soft。硬限制固定 hard，软限制固定 soft。
5. constraint_kind：硬限制固定 semantic_object，软限制固定 fuzzy_quality。
6. severity：严重度。hard 可 critical/high/medium；soft 通常 medium/low。
7. atoms：hard 下的具体违规 atom 列表。
8. trigger_groups：违规依赖用户状态时才写；自足性禁止事项输出空数组。
9. negative_groups：硬限制的违规表达组。必须同时包含受限对象 main 和违规动作/违规方向。不能只有“保证”“承诺”而没有对象。
10. safe_groups：安全翻转表达组。必须复用同一受限对象，并表达不能保证、以实际规则为准、用户自行操作、安全优先、稍后联系等安全做法。
11. quality_dimension：软限制维度，例如 tone、brevity、non_repetition、interaction、clarity。
12. metric：软限制度量。可写 name、direction、threshold_hint。
13. score_effect：软限制扣分或封顶方式。可写 weight、can_cap_score、cap。
14. description：软限制中文说明。
15. hard 表数量通常 3 到 8 类，最多 10 类。超过 10 类通常说明把 soft 或知识误放进 hard。
16. soft 表通常 3 到 6 类，不写 negative_groups，不写 safe_groups。

十、硬限制与软限制边界
1. hard 是明确负向对象：不能承诺结果、不能保证权限/结果/页面显示、不能人工改系统结果、不能代用户操作、不能越权处理、危险状态不能继续推进。
2. soft 是整体质量：语气自然、表达简洁、不重复、给用户回应机会、礼貌、清晰、节奏。
3. “不要说哈哈/嘻嘻”这类若复杂指令明确列出具体禁用词，属于 hard，因为负向对象明确。
4. “语气别太生硬”“不要太啰嗦”属于 soft，因为没有明确负向对象，只能整体判断。
5. 礼貌结束、祝福语、寒暄不是知识；一般也不是 hard，除非指令明确禁止某句话或某动作。

十一、五步输入边界
1. 第 1 步只看 original_complex_instruction；补图只看 original_complex_instruction 与 current_graph_core。
2. 第 2 步只看 original_complex_instruction；补知识只看 original_complex_instruction 与 current_knowledge_table。
3. 第 3 步只看 original_complex_instruction；补限制只看 original_complex_instruction、current_hard_constraint_table、current_soft_constraint_table。
4. 第 4 步只看 atom_transport.entries 中的 atom_id、atom_source、parent_id、atom_name、atom_text、requested_slots 以及已有相关组。
5. 第 5 步只看 atom_transport 中已有 element、activation 触发种子和 role_aware_element_hints，不看复杂指令全文。客服侧只扩已有 pool；用户触发侧可以新增 source_text trigger group，但只能表达原触发条件的同义用户说法，不能新增事实或新意图。

十二、atom_transport 字段词典
1. atom_id：本地生成的稳定 atom 标识。输出必须原样返回，不能改写、缩短、翻译。
2. atom_source：atom 来源。允许 activation、node_atom、knowledge、hard_constraint、soft_constraint。
3. parent_id：父节点、父知识或父限制 ID，仅用于理解归属，不能当成新事实。
4. atom_name：atom 中文短名称。
5. atom_text：atom 中文语义文本，是元素化的主要依据。
6. requested_slots：本批次要求输出哪些组。只输出 requested_slots 中列出的组。
7. 已有组：如果输入已有 selector_groups、correct_groups、negative_groups、safe_groups、element_groups，表示可参考的初稿；可以修正短语形态，但不能改变 atom 结论。

十三、各 atom_source 的槽位含义
1. activation：只输出 trigger_groups。trigger_groups 表示用户触发条件，不表示客服动作。
2. node_atom：只输出 element_groups。element_groups 表示客服动作是否完成；节点 atom 不允许 fact=true。
3. knowledge：输出 selector_groups 与 correct_groups。selector 召回对象属性，correct 加正确 fact。除非 requested_slots 明确要求，否则第 4 步不要新增 wrong_groups。
4. hard_constraint：输出 trigger_groups、negative_groups、safe_groups。negative 查违规，safe 查安全翻转。
5. soft_constraint：输出 element_groups。只表达质量维度，不写 fact，不写 negative/safe。

十四、常见不可复现错误
1. 把完整指令句当 element.value，导致召回极窄。
2. 把所有词都标 main，导致召回过严。
3. correct_groups 只有“3 秒以内”“12 次”这种 fact，没有同组对象 main，导致串项误杀。
4. selector_groups 放入正确值，导致错误事实也被当成未召回。
5. value_check 同时写 wrong_groups，导致数值错误被双重误判。
6. hard_constraint_table 爆炸到几十条，说明把所有“不能承诺”泛化成百科。
7. hard 只有“承诺/保证/一定”，没有受限对象，导致任何承诺语都被误杀。
8. safe_groups 没有复用对象，只写“以实际为准”，导致无法抵消同对象风险。
9. soft 项进入 hard，导致自然度、字数、重复被当成硬违规。
10. 元素扩张阶段新增事实、改数字、改时间、扩出反义词，导致完美图不可复现。

十五、二次补充机制的字段语义
1. local_supplement_hints：本地通用词典和结构审计输出的缺口提示，只在 supplement_core_graph_only、supplement_knowledge_table_only、supplement_hard_and_soft_constraint_tables_only 中出现。
2. hint_source：提示来源，表示本地根据通用客服动作族、通用事实槽或通用边界质量形状做了审计。
3. dictionary_scope：词典作用域，必须是跨任务通用形状，不能当作业务事实。
4. coverage_families：可能覆盖或遗漏的通用语义族。gap_type 为 candidate_missing 只表示可能缺口，不表示必须补。
5. structural_gaps：主图结构缺口，例如缺边、缺关系组、缺终止策略。它只能指导主图结构修复。
6. repair_hint：本地给模型的修复提醒。它不能替代原指令，不能被原样复制成节点、知识或限制。
7. 二次补图必须按“原指令依据优先、当前图复用优先、词典提示最后核对”的顺序工作。
8. 二次补表必须区分完整回写和 patch-only：知识补表完整回写 knowledge_table；限制补表只输出 patch；主图补充完整回写 graph_core。

十六、输出语言与 JSON 规则
1. 只输出本阶段要求的合法 JSON 对象；不能输出 Markdown、解释、代码块、注释或省略号。
2. 必须使用英文双引号；字段和数组元素之间必须有英文逗号；不能有尾随逗号。
3. 除 JSON 字段名、枚举、ID、URL、模型名、代码型标识外，所有人类可读语义内容必须用简体中文。
4. 输入若含英文标题、英文角色名或英文业务名，必须尽量转写为中文语义；确需保留的产品名可保留。

【二次补图/补表的硬边界空表规则】
- 本地通用词典只提供“缺口诊断”，不是答案库。LLM 必须回到 original_complex_instruction 找依据。
- hard_constraint_table 是否必须非空，只由原指令是否存在明确负向对象或硬边界决定，不能由知识表里的普通事实词决定。
- “超出职责范围、向同事确认后再回电、现在能回答的先回答”属于明确职责边界，通常需要 hard：禁止擅自编造、承诺处理或越权解答；safe 要复用同一对象并表达同事确认/回电/只答能答内容。
- “状态生效、申请规则、页面入口、完成数量、截止时间”等是知识事实，不自动产生 hard。
- 如果 hard 为空但原指令没有硬边界，应当允许；如果原指令有硬边界，二次补表优先围绕证据片段补 1-3 条，不要百科化。

十七、容易被误解字段的执行语义
下面字段不能只按字面理解，必须按“本地 evaluator 如何使用它”理解：
1. name：只供人读和报告展示，不参与严格召回；不要把关键事实只放在 name 而不放进 atom.text 或 element group。
2. text：本地构造 atom_transport 的主要语义来源，会影响后续 element 拆分；必须写完整一件事，但不能把多个任务混成一段。
3. description：只解释原因和边界，通常不参与严格命中；不要把唯一判断依据只放 description。
4. metadata.notes：仅记录生成说明和低风险备注，不参与评分；禁止放测试答案、样本标签、负包错误说明。
5. required：表示“在对应触发条件成立后是否必须履约”，不是“这个字段重要”。main 节点 required=true；用户追问触发的 faq 通常 required=false，但触发后其中 atom 可 required=true。
6. severity：表示该项错误对总分或审核优先级的影响，不表示该项是否存在。critical=会严重破坏任务或越权；high=关键事实/关键动作；medium=普通主线；low=轻微质量。
7. weight：数值版 severity。5≈critical/high，3≈medium，1≈low。没有把握时宁可用 medium/3，不要所有项都写 5。
8. condition：只写触发或成立条件，不写结论本身。例如“用户询问入口”是 condition；“告知入口在页面右上角”不是 condition。
9. trigger_hint：给人和本地修复器理解触发语境，不能替代 trigger_groups；它要短而清楚，避免“根据情况处理”这种空话。
10. source / target：只用于节点边，必须指向 node_id，不能指向 atom_id、knowledge_id 或 constraint_id。
11. relation / type：用于本地关系扣分，不是自然语言说明。两者必须同值，避免 compiler 兼容分歧。
12. suppress_nodes：表示某终止/暂停状态触发后不应继续强推的节点；不是“删除这些节点”，而是评分时允许这些节点不再必达。

十八、五步契约的内涵，不只是阶段名称
1. 主图阶段的“动作”是客服在对话中要完成的可观察行为，例如确认身份、告知目的、询问状态、回应追问、结束通话。动作可以引用业务对象，但不判断业务值真假。
2. 知识阶段的“事实”是客服说法中的可判真假的内容，例如数字、时间、范围、路径、适用条件、结果方向。事实必须有对象和属性，不能只有数值。
3. 限制阶段的“负向对象”是被禁止触碰的对象加违规动作，例如“系统结果+保证”“职责范围外问题+擅自解答”。只有“保证/承诺”这类动作词不构成完整 hard。
4. 一级元素阶段的“拆短语”是把一个 atom 拆成 evaluator 可找证据的槽，不是重新理解业务，也不是总结一句话。
5. 二级元素阶段的“扩 pool”是给已有短语加等价说法，不是扩知识、补流程、加限制或改事实。
6. 补图/补表的本质是“对当前阶段的输出做缺口修复”，不是第六步，也不是允许跨表混写。

十九、group 与 slot 的强语义矩阵
1. element_groups：用于节点履约或软质量。节点场景下命中表示客服做了该动作；软质量场景下命中表示出现某类质量表达或质量问题线索。
2. trigger_groups：用于判断某条件是否被用户状态触发。它不代表客服应该说什么，只代表是否进入某分支或某限制扫描语境。
3. selector_groups：用于判断客服是否在谈某个事实对象/属性。命中 selector 只说明“说到了这个事实”，不说明说得对。
4. correct_groups：用于判断客服对该事实说得是否正确。它必须包含对象/属性 main 和正确 fact，避免把别的对象的正确值串进来。
5. wrong_groups：用于明确相反事实的快速识别。凡是数字、时间、金额、比例、区间、阈值，错误不穷举，交给 value_check。
6. negative_groups：用于硬违规扫描。它必须是“对象 + 违规动作/方向”的组合；缺对象会误杀所有承诺语，缺动作会误杀所有对象说明。
7. safe_groups：用于识别安全翻转。它必须复用 negative 的同一对象，否则无法证明客服是在同一边界上安全处理。

二十、local_supplement_hints 完整字段词典
1. hint_source：本地提示生成来源。只说明提示由哪类通用审计器产生，不是业务证据。
2. dictionary_scope：本地词典作用域。若写 task_agnostic，表示只允许作为通用形状提醒，不能复制成任务事实。
3. allowed_use：本提示允许如何使用。模型只能按这里的用途检查缺口。
4. forbidden_use：本提示禁止如何使用。若 forbidden_use 与 repair_hint 冲突，以 forbidden_use 为准。
5. coverage_families：一组通用语义族提示。它们是候选审计结果，不是最终 schema 项。
6. family_id：通用语义族 ID，只用于定位提示，不应输出到最终图表。
7. family_name：通用语义族中文名，只用于理解，不应机械复制成 node.name、knowledge.name 或 constraint.name。
8. instruction_signal：原指令中是否出现该语义族线索。true 只表示需要复核，不表示一定要补。
9. current_graph_signal / current_table_signal：当前图表中是否已有相近线索。false 不等于必须补，必须回原指令确认。
10. gap_type：缺口类型。candidate_missing=可能缺失；covered_or_weak=已有或弱覆盖；must_review_empty_hard=原指令疑似有硬边界但 hard 为空，必须复核。
11. instruction_evidence_snippets：原指令中的短证据片段。它只能帮助回看原文，不能独立变成完整事实或完整限制。
12. repair_hint：修复建议。它描述修复方向，不是最终输出文本。
13. structural_gaps：结构缺口，只对主图有效，例如缺 edge、缺 relation_group、缺 terminal_policies。
14. hard_constraint_required_by_instruction.required：本地根据通用硬边界信号判断原指令是否可能要求 hard。true 时优先复核；false 时允许 hard 为空。
15. hard_constraint_required_by_instruction.signals：硬边界证据片段，必须从中抽出“受限对象+违规动作+安全翻转”才可补 hard。
16. empty_hard_policy：hard 空表策略。它告诉模型什么时候不能因为普通事实词强造 hard。

二十一、枚举值和阈值的语义，不得只抄字段
1. activation.mode=always：没有用户额外触发也应默认发生。用于开场和主线必达。
2. activation.mode=condition：用户状态或业务条件成立才发生。必须配 trigger_hint 或 trigger_groups 说明条件。
3. activation.mode=user_triggered：用户主动问到才发生。不能把主线必讲事实偷放成 user_triggered。
4. activation.mode=optional：有助于质量但非必达。不要用 optional 逃避主线任务。
5. edge.before：弱顺序，错序轻扣。
6. edge.required_after：强顺序和强后继，前项完成后后项应完成。
7. edge.optional_after：允许后继缺失，不应当作必达流程扣重分。
8. edge.condition_on：条件路径，必须配 condition 或由目标节点 activation 说明。
9. edge.terminal_after：到达目标状态后允许结束。
10. edge.suppress_after：触发后抑制后续节点必达性。
11. relation_group.sequential：整体顺序检查。
12. relation_group.any_of：多条可选路径中命中一条即可。
13. relation_group.exclusive_branch：互斥分支，不能要求所有分支同时完成。
14. relation_group.optional_parallel：并列补充，不严格要求顺序。
15. relation_group.all_of：都应覆盖，但顺序不严。

二十二、输出前自检清单
1. 每个字段是否知道“本地程序拿它做什么”？若不知道，不能只写字段名。
2. 每个 group 是否自足？有 fact 是否同组有对象 main？
3. 每个 hard 是否同时有受限对象、违规动作、安全翻转？
4. 每个 value_check 是否没有 wrong_groups/wrong_examples？
5. 每个补充项是否能在 original_complex_instruction 中找到依据？
6. 是否把 description、name、metadata 当成了评分依据？若是，必须把判断语义移入 atom.text 或对应 group。


二十一、角色感知 element 派生原则
1. element 不应只从任务语义标签里机械切词，而应从“最可能出现的对话话语”里派生。
2. 客服侧话语由同一系统生成，表达趋同。因此 node_atom、knowledge、hard safe/negative、soft 的 element 应先构造最可能客服答话，再从该答话拆对象、动作、属性、事实值、违规动作或安全翻转。
3. 用户侧话语开放且不可控。因此 activation trigger 不能只按 trigger_hint 一句话切词，必须先生成大量可能用户说法，再从这些说法中抽共同触发槽，并把其它同意图说法放进 pool。
4. 对知识事实，selector/correct 不应从抽象事实标题拆，而应从客服最可能正确回答拆。selector 找对象与属性，correct 复用对象属性并加入正确 fact。
5. 对 hard 限制，negative 来自可能违规客服说法，safe 来自期望安全客服说法；二者必须围绕同一受限对象，pool 不得互相混入。
6. 这个原则的目标是让 element 接近真实评估文本，而不是接近 schema 编写者的抽象任务标题。


【角色感知 element 派生修正】
1. atom_transport 只传 atom_id、atom_source、atom_text、requested_slots 和极简派生模式，不承载大段模拟话术。
2. 客服侧 element 来自模型最期望客服说出的答话：node_atom、knowledge、hard negative/safe、soft 都先按客服答话拆 element，再在第五步对 element.pool 做严格等价扩张。
3. 用户侧 trigger 不能只靠一句 trigger_hint，也不能只对 element 做同义词扩张。第五步必须先生成多条可能用户话术 text，再把每条 text element 化为一组 trigger_groups。多组之间是 OR，任意一组命中即触发。
4. 第四步一级元素主要服务客服侧。activation 在第四步只生成最小触发种子，不做大量用户 pool。

【当前补图优先级】
当生成结果与理想图严重不对齐时，优先把问题修进二次补图或二次补表，而不是靠报告解释：
1. FAQ 合并过度：二次补图必须按用户问题对象拆成多个 faq 节点。
2. FAQ 事实进入主线：二次补图必须把只在追问时回答的事实移出 required main。
3. 条件节点误入 required sequential：二次补图必须修 relation_groups，避免未触发分支被当缺失。
4. 用户 trigger 覆盖不足：第五步必须先扩 likely_user_texts，再逐条 element 化为 OR trigger group。
5. hard 重复或过泛：二次补表必须 already_covered/remove_constraint_ids，不新增重复 hard，不使用过宽对象替代具体对象。

二十二、element 来源与质量门（强化）
1. element 的来源不是 schema 标题，也不是任务标签，而是“最可能出现在评估文本中的话语”。客服侧来自系统最期望答话；用户侧来自可能用户话术。
2. node_atom、knowledge、hard negative/safe、soft 必须先在心里构造一句自然客服答话，再拆 element。不要从“说明/处理/问题/规则/知识库/情况”这类抽象任务词直接切 element。
3. activation 与用户状态 trigger 不能靠单个模拟 text，也不能靠“用户/用户/对话对象/我/对方/客户”触发。第五步必须生成多条 source_text，每条 source_text 变成一组 trigger_groups；组内是 AND，组间是 OR。
4. 用户 trigger group 必须至少包含状态、意图、对象或极性中的一个可区分 main，例如“不想执行任务”“无法执行任务”“不是负责人”“不知情”“开车”“未显示”“已设置”“想取消”。参与者词只能辅助，不能作为唯一 main。
5. node_atom 不做事实真假核验。若节点动作里出现数字、时间、金额、路径，可作为履约辅助 element，但事实真假必须在 knowledge 的 fact/value_check 中核验。
6. knowledge element 必须能召回错误事实。selector 只放对象+属性，不放正确值；correct 放正确值/方向；数值时间金额走 value_check；非数值方向错误可用 wrong_groups。
7. hard element 必须“具体对象 + 违规动作”。禁止用过泛对象替代原文具体对象：能写“折扣券/优惠券”就不要写“权益”；能写“开车”就不要写“安全状态”；能写“职责范围外问题”就不要写“问题”。
8. pool 覆盖率不是质量本身。pool 必须严格等价；不允许为了显得丰富而扩出上位词、相反极性、额外事实、额外步骤或更宽对象。
9. 若一个 group 单独看不能判断对象/动作/属性，必须重写。若一个 element.value 只有“问题、情况、处理、规则、进行、相关、内容、信息、知识库”等抽象词，且同组没有具体对象和状态，必须重写。
10. 二次补图要服务 element：FAQ 必须按用户问题对象拆；信息获取 atom 写成“确认/获得 X 信息”；条件节点 trigger_hint 必须足够生成用户 source_text。


【最后一轮补图强约束：只做可执行修复，不写展示话】
1. 主线必达节点必须 activation.mode=always。若一个节点承载核心告知、核心说明、核心升级/变更内容，不得写成 user_triggered 或 condition。
2. condition、user_triggered、faq、out_of_scope、terminal 节点不得放入 required sequential；只能通过 condition_on、exclusive_branch、any_of、optional_after 或 terminal_after 连接。
3. 若用户状态是“忙/没空/不方便但允许简短沟通”，处理节点后必须回流主线；不得用 suppress_after 压制主线。只有明确终止状态才 suppress 主线。
4. 若用户状态是安全风险、无法沟通、坚持拒绝或明确终止，terminal 节点必须有 activation trigger_hint 和至少一组用户侧 trigger_groups，不能 optional 空触发。
5. 信息获取类 atom 不要只写“询问 X”。必须写成“确认或根据用户已提供信息获取 X”。如果用户已主动给出状态，客服可以直接使用，不必重复问。
6. FAQ 必须一问一类；一个 FAQ 节点只回答一个问题对象。若一个节点里混有多个互不等价的问题对象，必须拆节点。
7. out_of_scope trigger 不得用“其他问题/问题/情况/事情”作为 main。必须是明确职责、权限、边界、不可确认、需转相关人员等状态。
