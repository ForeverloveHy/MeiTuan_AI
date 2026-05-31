# 03 正负包样例：一条数据如何走完整个评估流程

本文档用商家任务中的一组正负包作为例子，说明 SCEG 如何把一条对话从 JSON 数据评估到最终报告。

选用样例：

| 样本 | 文件 |
| --- | --- |
| 正包 | `data/dialogues/positive_pack/merchant/merchant_01_positive.json` |
| 负包 | `data/dialogues/negative_pack/merchant/merchant_01_negative.json` |
| 离线图 | `runs/graphs_offline/course_publish_upgrade_v1.json` |
| 示例报告 | `runs/merchant_example/all_reports_merged.json` |

这组样例对应“课程发布页直播升级通知”任务。客服需要确认身份、说明后台已走低延迟、解释临时开启原因、告知后续发布页升级、说明标准直播和低延迟直播区别、询问发布方式、检查学员端费用、添加企业微信并礼貌结束。

## 1. 状态图中的核心评估标准

商家离线图 `course_publish_upgrade_v1.json` 中，主流程节点包括：

| 节点 ID | 节点名称 | 作用 |
| --- | --- | --- |
| `identity_confirmation` | 身份确认 | 确认对方是否为机构/校区负责人，不是本人则请其转达。 |
| `confirm_previous_awareness` | 确认是否知情 | 询问商家是否知道后台已为其走低延迟线路；若不知道，解释临时开启原因。 |
| `inform_upgrade` | 传达升级内容 | 告知发布页会分开显示标准直播和低延迟直播两个选项，并说明选择方式。 |
| `explain_price_difference` | 说明价格差异 | 说明标准直播更便宜，低延迟费用略高，因为带宽和节点保障更强。 |
| `check_publish_method` | 确认前端是否可见 | 询问商家通过哪种方式发课。 |
| `guide_third_party_enable_low_latency` | 引导第三方系统开通低延迟直播 | 用户触发第三方系统问题时，慢慢引导开通。 |
| `check_student_fee` | 检查学员端费用 | 提醒确认低延迟直播也适用已设置费用，或引导设置学员端费用。 |
| `add_wechat_followup` | 企业微信添加 | 告知稍后通过企业微信添加并请通过验证。 |
| `polite_close` | 结束通话 | 祝商家课程顺利、招生满满，并结束通话。 |
| `handle_busy` | 处理商家忙碌 | 用户忙碌时，说明就一分钟、保证简短。 |
| `handle_driving` | 处理商家在开车 | 用户开车时，礼貌说稍后再打并挂断。 |

知识表包括：

- 学员端费用知识；
- 第三方系统开通知识；
- 低延迟直播适用场景知识。

限制表包括：

- 不能承诺折扣券或优惠券；
- 不能承诺系统一定显示或自动开通；
- 用户开车时不能继续推进通话。

## 2. 正包原始对话片段

`merchant_01_positive` 的核心片段如下：

```text
客服：请问您是贵机构或贵校区负责人吗？
用户：我是负责人，你说重点。
客服：我先确认一下，后台其实已为您走低延迟，您知道吗？
客服：当时前端当时未开放，所以临时开启低延迟，是为保障质量、保障音视频同步和白板同步。
客服：后面课程发布页会做升级，并新增标准直播和低延迟直播独立选项。
客服：标准直播成本更低，低延迟费用会略高，因为带宽和节点保障更强。
客服：您现在是通过课程发布页还是第三方系统发课？
客服：如果已设置学员端费用，请确认低延迟直播也适用该费用。
客服：稍后我会通过企业微信添加您，请您通过一下。
客服：祝您后续课程顺利、招生满满。
```

这条对话的特点是：用户语气比较强势、要求讲重点，但客服没有越界承诺，也没有遗漏核心流程。

## 3. 负包原始对话片段

`merchant_01_negative` 的预设错误是流程缺失：没有完成身份确认。

负包中的 `injected_errors` 为：

```json
{
  "error_family": "flow_missing",
  "node_id": "identity_confirmation",
  "requirement_id": "identity",
  "evidence_span": "",
  "evaluability": "local",
  "expected_detector": "schema_executor"
}
```

负包片段如下：

```text
客服：后面课程发布页会做升级，并新增标准直播和低延迟直播独立选项。
用户：你直接说重点。
客服：我先把后面的事项说明清楚。
用户：这次主要影响我们怎么发课？
客服：我先确认一下，后台其实已为您走低延迟，您知道吗？
客服：当时前端当时未开放，所以临时开启低延迟，是为保障质量、保障音视频同步和白板同步。
客服：后面课程发布页会做升级，并新增标准直播和低延迟直播独立选项。
```

可以看到，客服直接进入业务说明，没有先问“是否为机构或校区负责人”。这不是一句错误话术，而是一个目标履约证据缺失。

## 4. 第一步：读取离线图

在离线 demo 中，系统读取：

```text
runs/graphs_offline/course_publish_upgrade_v1.json
```

`demo_runner.run_offline_project()` 会先保存 `source_graph.json`，再进行：

```text
_legacy_to_latest()
→ compile_state_graph()
→ lint_and_repair_schema()
→ StateGraph.from_dict()
```

这一步的意义是：即使离线图来自 LongCat-Flash-Lite 的历史生成结果，也要先经过本地结构检查和编译，确保 graph 能被 evaluator 稳定执行。

## 5. 第二步：读取对话并过滤 domain

系统读取 `data/dialogues`。由于该目录同时包含商家和骑手数据，`_domain_compatibility_filter()` 会根据 graph 的 `metadata.domain=merchant` 保留商家对话。

这一步避免把商家课程发布图错误套到骑手合同通知对话上。

## 6. 第三步：抽取证据单元

`EvidenceExtractor` 会把每轮对话转成 `EvidenceUnit`：

```text
turn_index
speaker
原始文本
normalized 文本
显式数字 / 范围
```

例如正包第一句会变成：

```text
turn_index = 0
speaker = assistant
text = 请问您是贵机构或贵校区负责人吗？
```

负包第一句会变成：

```text
turn_index = 0
speaker = assistant
text = 后面课程发布页会做升级，并新增标准直播和低延迟直播独立选项。
```

EvidenceExtractor 不判断业务含义，只保留格式化证据。

## 7. 第四步：节点履约判断

`GraphEvaluator` 对每个 active 节点执行 requirement 和 evidence group 匹配。

### 7.1 正包的身份确认节点

状态图中 `identity_confirmation` 的 requirement 是：

```text
identity：确认对方是否为机构/校区负责人，若不是则请其转达
```

正包中客服第一句：

```text
请问您是贵机构或贵校区负责人吗？
```

命中该 requirement，因此节点结果为：

```text
identity_confirmation / 身份确认：已完成，score = 1.0
```

### 7.2 负包的身份确认节点

负包没有出现身份确认问句。它一开始就说：

```text
后面课程发布页会做升级，并新增标准直播和低延迟直播独立选项。
```

这句话命中了 `inform_upgrade`，但没有命中 `identity_confirmation.identity`。

因此该节点结果为：

```text
identity_confirmation / 身份确认：缺失，score = 0.0
```

这就是负包预设流程缺失能够被本地 evaluator 识别的关键。

## 8. 第五步：知识和限制判断

在这一对样例中，知识和限制部分没有发现问题。

正包评估结果：

```text
knowledge_events = []
constraint_events = []
knowledge_score = 100.0
constraint_score = 100.0
```

负包评估结果也没有知识冲突或限制违规：

```text
knowledge_events = []
constraint_events = []
knowledge_score = 100.0
constraint_score = 100.0
```

这说明系统不是为了让负包失败而随便找错，而是准确定位到“流程缺失”这一类问题。

## 9. 第六步：结构关系评分

商家图中主流程要求身份确认在前，确认知情、升级说明、价格差异、发布方式等节点在后。

正包中身份确认出现在最前面，后续节点依次出现，因此结构正常：

```text
relation_score = 100.0
```

负包中后续节点出现了，但前置身份确认缺失，因此结构分降低：

```text
relation_score = 55.0
```

这体现了状态图的作用：系统不仅看“说没说某些内容”，还会看前置流程是否合理。

## 10. 第七步：四维分数

`runs/merchant_example/all_reports_merged.json` 中，这两条样例的四维分数如下。

### 10.1 正包分数

| 维度 | 分数 |
| --- | ---: |
| 总分 | 100.0 |
| 节点完成度 | 100.0 |
| 结构关系 | 100.0 |
| 知识正确性 | 100.0 |
| 限制合规性 | 100.0 |

正包没有 cap，也没有仲裁候选。

### 10.2 负包分数

| 维度 | 分数 |
| --- | ---: |
| 总分 | 52.0 |
| 节点完成度 | 55.0 |
| 结构关系 | 55.0 |
| 知识正确性 | 100.0 |
| 限制合规性 | 100.0 |

负包触发了两个 cap：

```text
cap = 72.0：核心履约证据缺失且存在部分完成节点
cap = 52.0：负包预设流程缺失已命中
```

这说明系统把“流程缺失”视为任务成功门槛，而不是让其他说对的内容把总分平均拉高。

## 11. 第八步：正负包验收

### 11.1 正包验收

`DatasetInterface` 对正包的验收结果是：

```text
result = 本地通过
passed = true
reason = 正包本地通过：未发现必需节点缺失、事实冲突、限制违规或条件转场问题
```

换句话说，这条正包不只是分数高，而且没有任何硬性失败事件。

### 11.2 负包验收

负包的验收结果是：

```text
result = 本地通过
passed = true
reason = 负包本地通过：预期错误已被评估器识别
matched_expected = flow_missing / identity_confirmation / identity
```

这里的“本地通过”不是说客服表现好，而是说这条负包作为测试样本通过了验收：它故意设置的错误被系统识别出来了。

## 12. 第九步：报告生成

`ReportExplainer` 会把上述结构化结果翻译成中文解释。对于负包，它会告诉评委：

```text
样本标注的 flow_missing 已经被评估器识别。
流程缺失类错误没有单句错话，问题在于目标履约证据缺失。
目标节点是 identity_confirmation，目标小任务是 identity。
```

在详细报告中，评委可以继续展开：

- 目标节点状态；
- 小任务是否命中；
- 证据组期望；
- 当前命中原话；
- 同节点附近客服原话；
- 最终 cap 和验收原因。

这就是 SCEG 的可解释性：不是只给一个“52 分”，而是告诉你为什么是 52 分、缺的是哪一个节点、对应哪个 requirement、证据在哪。

## 13. 这组样例体现的方法价值

这组正负包说明了三个关键点。

第一，系统能识别“没有单句错话”的错误。负包没有说错知识，也没有违规承诺，但它漏掉了身份确认。传统关键词或整段相似度很容易忽略这种流程错误；状态图 evaluator 可以直接定位。

第二，系统不会因为后续内容正确就放过前置流程缺失。负包虽然说了升级内容、低延迟原因等信息，但身份确认是前置节点，因此仍然被判为负包预设问题命中。

第三，评估结果可解释。报告不是黑盒打分，而是给出节点、requirement、证据组、cap 和验收追踪。

## 14. 可以向评委这样解释

这对样例可以这样口播：

> 正包中，客服先确认对方是否是机构或校区负责人，再说明后台已走低延迟、升级内容、价格差异和后续处理方式，所以状态图里的主流程节点全部完成，知识和限制也没有问题，最终本地通过。
>
> 负包中，客服一上来直接讲课程发布页升级，没有先确认身份。这个错误不是一句错话，而是前置流程缺失。我们的 evaluator 会把对话证据映射到状态图节点，发现 `identity_confirmation.identity` 没有命中，因此触发流程缺失 cap，并且和负包 `injected_errors` 对齐，最终判定这个负包本地通过，即预设错误被系统识别。
