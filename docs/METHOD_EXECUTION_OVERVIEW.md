# SCEG 方法执行梳理：状态图 + 结构化证据 + 二次建图

## 1. 总体思想

SCEG 面向复杂客服指令对话评估。它不让大模型直接给整段对话打分，也不在本地代码中写业务词典，而是把复杂指令先转成可执行的结构化评估标准，再由本地评估器高速执行。

核心链路是：

```text
复杂指令
→ LongCat 初次建图
→ Schema Linter / Schema Compiler
→ 本地 schema gap audit
→ LongCat 二次补图 repair
→ 本地 GraphEvaluator 四维评估
→ LocalSecondFilter 二筛
→ 必要灰区 LongCat 仲裁
→ 中文可解释报告
```

这句话可以概括为：**LongCat 离线建标准，本地跑证据，LongCat 只补图和裁灰区。**

## 2. LongCat 初次建图

输入是一段复杂客服指令。LongCat 初次输出一份状态图 schema，主要包含：

- `nodes`：客服动作节点、触发方式、requirements、evidence_groups；
- `knowledge_table`：事实知识、对象锚点、支持表达、反向表达；
- `constraint_table`：禁止承诺、越界处理、安全边界，其中 `violation_scope` 显式给出 protected_objects / forbidden_actions / safe_actions / ambiguous_zone；
- `terminal_policies`：无法继续、拒绝继续、安全风险、明确终止等场景下的转场或压制策略；
- `relation_groups`：主线必做、无序必做、部分有序、用户触发分支等结构关系。

本地代码不把任务词写成规则，只消费 LongCat 输出的结构化 schema。

## 3. Schema Linter 与 Compiler

`schema_linter.py` 是结构质检器。它只修通用结构问题，不补业务事实：

- branch / FAQ 不应污染主线；
- 空 prohibited 不能当普通违规规则；
- 软要求、结束语、风格项不应压死正包；
- support / refute 需要对象锚定，避免兄弟知识串扰；
- `wrong_statement` / `evidence_span` 不得进入可执行规则；
- 过宽 evidence group 会被提示或收紧，避免只靠主题词命中。

`schema_compiler.py` 把 schema 编译为本地执行结构，仍然只使用 graph、配置和对话证据，不读取负包答案作为判分条件。

## 4. 二次建图：Schema Gap Audit → LongCat Repair

这次补上的关键思想是：**本地系统可以发现图缺口，但不在本地补业务答案；真正的补图仍交给 LongCat 完成。**

流程如下：

1. LongCat 先根据复杂指令生成初始 schema。
2. 本地 `schema_repair_audit.py` 做结构审计，只输出图缺口，例如：
   - target 节点缺失；
   - knowledge / constraint 表为空或缺对象锚点；
   - 用户触发分支被错误放进主线；
   - 限制规则缺少 `violation_scope`；
   - 终止策略没有压制不适用主线节点；
   - evidence group 过窄或过宽，无法支持自然表达。
3. 系统把“原始复杂指令 + 当前 schema + 本地结构审计 + binding_hints 的高层覆盖意图”交给 LongCat。
4. LongCat 返回一份完整新 schema，而不是 diff。
5. 本地重新 linter / compiler / evaluator。

这里最重要的边界是：

- 本地 audit 只指出结构缺口，不写业务事实；
- positive 的 `source_positive_design` 只能作为覆盖意图，不是标准答案；
- negative 的 `wrong_statement` / `evidence_span` 不进入建图 prompt，也不能被复制到规则；
- 真实生产时走 LongCat API；本地 CI 没有 API 时，可以用外部模拟目录模拟 LongCat 返回，但模拟目录不进入 `data/`。

## 5. 本地四维评估

`GraphEvaluator` 读取对话后生成四类账本：

- 节点履约：requirement / evidence_group 是否命中；
- 关系结构：顺序、分支、终止是否合理；
- 知识事实：对象锚定后判断支持、冲突、证据不足；
- 限制边界：触发条件、`violation_scope`、prohibited、安全否定、终止策略。

评分不是关键词直判，而是把对话证据映射到 schema，再计算四维分数和 cap。业务对象来自 schema，本地只执行通用语言算子。

## 6. 正负包验收

`DatasetInterface` 根据样本类型做验收：

- 正包：核心目标应完成，不能出现事实冲突或高风险限制违规；
- 负包：必须识别 injected error 对应的问题类型；
- 维度对齐：如果目标维度没有直接命中，但同一句已产生本地严重事件，可用于关闭待仲裁项；
- `evidence_span` 只用于追踪同一句和生成解释，不可单独判分。

## 7. LLM 中文仲裁与本地二筛

`OracleRouter` 只把本地无法稳定判断的候选送入队列。

`local_second_filter.py` 先做本地二筛：

- 明显支持 / 安全的候选本地合并；
- 同类候选合并；
- 有 schema 锚点、有对话证据、语义方向不稳的候选才送 LongCat；
- 空证据、重复候选、明显已支持候选不送审。

`llm_verifier.py` 的输出只允许三类：

- 问题成立；
- 问题不成立；
- 证据不足。

LongCat 仲裁不是重新评价整段对话，而是判断一个具体灰区是否落入 schema 定义的知识冲突或限制违例范围。

## 8. 工业可行性

这套方法高度适合工业落地，原因是计算成本被拆开了：

第一，主要 LLM 成本发生在离线建图阶段。复杂指令通常比对话样本少得多，LongCat 初次建图和二次 repair 可以离线执行、缓存和复用。

第二，正式评估是本地执行。节点命中、知识冲突、限制边界、关系结构都由本地 evaluator 跑，速度快、可复现、可批量回归。

第三，送审前有本地二筛。系统不会把所有样本都送给 LongCat，只把少量有 schema 锚点但中文语义不稳的候选送审，所以实际有效送达 token 很少。

第四，报告可以呈现这些指标。后续报告中可以展示建图调用次数、二次 repair 次数、LongCat token 统计、本地通过比例、待仲裁候选数量、二筛过滤数量和最终仲裁结论。评委能够看到成本不是被隐藏，而是被系统性量化。

## 9. 交付包数据边界

当前清理策略是：`data/` 只保留按正负包分类的模拟对话数据。

不应放入 `data/` 的内容包括：

- 已生成状态图；
- 回归输出；
- HTML 报告；
- 模拟 LongCat 返回；
- 缓存、临时目录、旧版本调试产物。

真实运行产生的图、报告和审计结果默认进入 `runs/`，便于复现但不污染原始数据。


## 工程运行补充：不限时 LongCat 与 1 秒计时

二次建图链路可能比普通本地评估更耗时，因此 v47 取消 LongCat 调用的默认应用层超时。系统仍会记录运行用时和 token 用量，但不会因为固定秒数阈值提前中断 LongCat 建图或补图。图形界面中的用时显示按 1 秒刷新，便于演示或长时间运行时观察进度。

### v48：计时心跳与 UI 线程隔离

图形界面的计时显示不再依赖模块切换或日志输出。运行开始时，界面主线程启动每秒一次的 heartbeat；后台 worker 只负责执行 LongCat 建图、二次补图和本地评估，不直接修改 Tk 控件。所有日志、进度、完成和失败事件都进入 UI 队列，由主线程统一消费。因此在 LongCat 网络调用或长文本生成期间，即使没有新的模块进度事件，计时标签也会持续按秒刷新。


## v49：LongCat 两次建图分段计时

界面运行时，系统把 LongCat 初次建图与 schema gap audit 后的二次 repair 建图分开计时。计时栏同时显示总用时、一次建图用时和二次补图用时；如果审计没有发现必须补图项，二次补图显示“未触发”。

这项改动不改变评估逻辑，也不在本地代码里增加业务词。它只是让离线 LLM 建图成本更透明，便于后续在报告中向评委说明：主要耗时集中在离线建图阶段，本地评估仍然是高速执行；二次建图是否触发、触发耗时和 token 用量都可以被记录。

## 无记忆二次建图与知识表质量审计

系统不把一次 LongCat 输出视为最终标准，而是在本地执行前增加 schema gap audit。这个审计并不修补业务答案，只判断图是否满足可执行条件：知识项是否具备对象锚点、属性锚点、支持值与反向值；比较型事实是否同时写出相反方向；refute 是否过短或只有 any；限制项是否拆出受保护对象、禁止动作、安全动作、触发范围和灰区；终止策略是否覆盖同义触发表达。

审计结果进入第二次 LongCat repair。repair agent 按无记忆原则工作，只使用当前复杂指令、当前 schema、审计缺口和高层 ID 绑定信息。它不能引用历史调试经验，也不能复制测试集错句。这样做的目的，是让泛化性来自建图协议本身，而不是来自本地代码记住某个商家或骑手样本。
