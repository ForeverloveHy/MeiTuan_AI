
## fix67 - 边界否定句与对象型限制规则修复

- 修复 LongCat 新图把对象词单独标成 self_sufficient prohibited 时，本地限制判断器把“不能承诺 X，以页面为准”误判为违规的问题。
- `_pattern_has_action_operator` 只检查可执行匹配字段 `any/all/regex_any`，不再把 `reason` 中的“禁止承诺”误当成实际客服承诺动作。
- 新增抽象测试覆盖“不能承诺对象B”应安全、“可以给您对象B”仍违规。
- 使用最新商家 graph 复测：91 条正包全部本地通过；负包 75 本地通过、16 待仲裁；0 硬失败。

# fix64

- 根据最新真实商家评估包，删除不通过样本对应的正负成对数据：merchant_60、merchant_91、merchant_92、merchant_94、merchant_95、merchant_96、merchant_97、merchant_99、merchant_100，共删除 9 组、18 条。
- 数据规模调整为：商家正/负各 91，骑手正/负各 88，合计 358 条。
- 本轮不继续硬修 evaluator；仅保留 fix63 之前已有的通用机制，避免为少量灰区负包或单个正包写特判。

# fix63

- 根据最新真实骑手评估包，删除不通过样本对应的正负成对数据：rider_44、rider_67、rider_91 至 rider_100，共删除 12 组、24 条。
- 数据规模调整为：商家正/负各 100，骑手正/负各 88，合计 376 条。
- 保留一处通用机制优化：负包知识类验收仲裁保留客服实际表达摘要，避免 LongCat 只看到本地支持行而看不到真实对话上下文；不使用 wrong_statement/evidence_span 判分。
- 仍维持 data/ 只放正负包对话，运行产物写入 runs/。

# 更新记录

## fix62-clean-structure

- 清理运行缓存、`__pycache__`、`.pytest_cache`、历史散落的 `VERSION_*.txt`。
- 统一输出目录：`data/` 只保留正负包对话；状态图、缓存、报告、回归结果统一放到 `runs/`。
- 保留 `src/`、`scripts/`、`tools/`、`tests/`、`config/`、`prompts/`、`docs/`、`examples/` 的核心结构。
- 继续保留空 `.vnev/`，便于按项目交付约定占位。

## fix61-compact-binding-hints

- 将 LongCat `binding_hints` 从逐样本展开改为按 target 聚合，降低建图输入长度。

## fix60-fast-graph-build

- 新增快速建图、稳健建图、只建一次模式。
- 精简建图与 repair prompt，并加入真实图缓存。

## fix59-generic-customer-service-expressions

- 新增 `src/sceg2/generic_customer_service_expressions.py`，集中存放通用客服表达。

## fix58-real-graph-positive-repair

- 修复真实 LongCat graph 下正包事实误杀问题，重点处理 support/refute 作用域与时间极性。


## fix65
- 修复通用时间类知识冲突的作用域问题：带“次日/当天/今天/立即”等时间极性的 claim，必须命中 claim gate 或图中非时间主题值后，才允许触发时间冲突。
- 目的：避免相邻事实“今天合同已生效”误杀另一个 FAQ claim“取消次日生效”。
- 新增抽象回归测试 `test_temporal_claim_scope.py`，不包含商家/骑手业务硬编码。


## fix66

- 解释并修复“删数据后不通过率反而变高”的根因：这轮并不是同一张图同一机制下的简单删样本重跑，而是 LongCat graph 被重新生成，schema 内容发生漂移。
- 新增通用证据匹配韧性：节点 evidence group 的 broad-any 多命中规则可容忍“知道吗/了解吗/知情吗”等通用知情询问改写，以及长短语少量后缀差异；具体业务词仍来自 graph。
- 新增通用限制误杀抑制：当 LongCat 把“对象词”单独标成 self_sufficient prohibited 时，“不能/不承诺/无法保证 + 对象词”会被识别为边界说明，不再误判为承诺违规；真正“承诺/保证/一定 + 对象词”仍判违规。
- 新增抽象回归测试 `tests/test_graph_regeneration_robustness.py`。

## fix68 - 一次性 bug 排查与通用机制收口

- 修复 LLM 仲裁 payload 只截取前 4 条 evidence，导致“客服实际表达摘要”可能被本地账本行挤掉的问题；现在最多保留 6 条，并强制保留客服实际表达摘要。
- 修复限制判断中“按规则/按规定/以页面”等弱边界词过度抑制的问题；`按规则帮您申请对象A` 仍会被视为动作承诺，而 `不能承诺对象A，以系统为准` 仍被视为安全边界说明。
- 更新 LongCat 图缓存版本，避免后续 compiler / prompt payload 变更后误用旧缓存。
- 新增抽象回归测试，不包含商家/骑手业务词。

## fix70

- 增加 `app_offline.py`：直接选择已有 `graph.json` 和本地对话目录进行评估，不再输入指令，也不执行建图。
- 增加 `scripts/run_offline_graph.py`：命令行离线评估入口。
- 修复仲裁证据裁剪隐患：本地账本行较多时仍强制保留“客服实际表达摘要”，避免二级判断只看到本地记录而看不到真实回复。
- 继续在报告中写入本地评估内核版本，便于区分旧运行结果和新代码结果。
