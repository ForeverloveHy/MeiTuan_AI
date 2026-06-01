# SCEG：复杂指令下多轮对话评估系统

> 项目队伍：膨胀神券一队  
> 赛题方向：命题二——复杂指令下多轮对话评估系统  
> 方法定位：Schema / State-Chart Evidence Grounding，即“结构化状态图 + 证据归因”的多轮对话评估框架。

## 1. 队伍与分工

评委老师们好！

我们是膨胀神券一队，队长是来自武汉大学 24 级马克思主义学院的何姚，组员是来自西安电子科技大学 24 级人工智能学院的沈晨旭。本项目负责的课题是命题二：复杂指令下多轮对话评估系统。

队长主要负责项目文书工作、模拟数据集构建、人工校验评估结果；组员主要负责代码与大模型的落地与调试。两位成员共同负责整个项目的方法设计、实验统筹和展示材料整理。

## 2. 项目概括

本项目不是直接让大模型给客服对话打分，也不是用关键词规则做简单匹配，而是先将复杂客服指令转化为可执行的状态主图、知识副表和限制副表，再由本地评估器对多轮对话进行高速、可解释、可追溯的结构化评估，并在少量灰区引入 LongCat 进行二次仲裁。

核心流程可以概括为：

```text
复杂客服指令
→ LongCat-Flash-Lite 离线建图
→ Schema Linter / Schema Compiler
→ 本地 Graph Evaluator
→ 本地二筛
→ 少量灰区 LongCat 二次仲裁
→ 中文可解释评估报告
```

## 3. 方法创新点

### 3.1 “一主图二副表”的 schema 建图设计

项目以节点状态图为主图，辅助知识副表和限制副表。

节点主图为每项业务动作或流程步骤提供了明确的“数字坐标”，使评估结果可以回溯到具体节点、具体 requirement 和具体证据。图与表分流的设计，让“流程完整、知识正确、严格合规”三大评估点都能够被量化、调控和解释。

其中：

- **状态主图**负责校验流程完整度、结构顺序和条件分支；
- **知识副表**负责校验客服是否说错业务事实；
- **限制副表**负责校验客服是否越界承诺、违规保证或突破合规边界。

这种结构比单纯状态机更贴近真实客服质检场景，因为复杂客服指令不仅包含流程，还包含知识说明、禁止事项、异常终止策略和用户追问分支。后续还添加了终止策略设计，详细可以见02_METHOD_AND_MODULES.md。

### 3.2 结构化证据的本地评估校验设计

本地校验确保整个数据集的评估主线严格落实在本地。数百条核心评估可以在个人电脑上数秒内完成，绝大多数样本不需要大模型逐条介入。

评估校验的核心，是将数据集中每一轮对话拆解为具有特定结构的证据集，再与图表中生成的 evidence group 进行匹配，从而判断：

- 流程节点是否命中；
- requirement 是否完成；
- 知识点是否正确；
- 限制点是否被违反；
- 异常终止和拒绝分支是否处理得当。

这样的设计使校验更加立体化。系统不只输出“命中/不命中”，还能够识别“部分命中”“语义相近但证据不足”“需要二次仲裁”等中间状态，因此具备将灰区样本送入二次仲裁的能力。

需要说明的是，副表下会直接设立对应每条知识点或限制点的证据组；而主图会先将节点拆分为多个小履约任务，再在每条 requirement 下设立证据组。因此主图具备更细粒度的给分和解释方式。该 requirement 细粒度拆分思想借鉴了 InFoBench 一类任务分解评价方法，项目原创部分主要体现在面向复杂客服指令的主图、副表、编译、执行和仲裁整合框架。

### 3.3 “二次补图”与“二级仲裁”的大模型协同设计

本项目并不让 LLM 直接替代本地 evaluator，而是把大模型能力放在最有价值的环节。

一方面，本地会对 LongCat 建图结果进行规范性校验和结构化编译；另一方面，如果第一次建图存在硬性缺失，本地可以发起二次补图，对状态主图、知识副表或限制副表中的缺项进行补充。

与此同时，本地评估器能够识别评估过程中的“灰度地带”，并通过本地二次加工，将最浓缩的局部待仲裁信息送到 LongCat 进行二次仲裁。

这套流程将大规模复核与筛选留给本地系统，只让大模型在少量、必要、局部的语义判断中发挥作用，从而尽可能用更少 tokens 实现更大价值。

## 4. 评估示例说明

项目中的评估示例可以在以下目录查看：

```text
runs/merchant_example/report_detail.html
runs/ridder_example/report_detail.html
```

这些示例使用 **LongCat-Flash-Lite** 模型进行离线建图和在线仲裁，使用 `data/dialogues` 中的模拟对话数据，并通过 `app_offline.py` 选择辅助模式调用生成。

从示例报告中可以看到：

- 项目已经经过正负包验收；
- 九成以上样本主要依靠本地机制直接通过；
- 其他灰区样本可以通过二次仲裁完成局部协调；
- 本地机制仍然是评估主线，LongCat 仲裁只作为灰区补充。

调试和演示时，状态图一般是离线预先生成的。一次完整建图通常需要 5 到 10 分钟不等；二次仲裁经过本地过滤筛选后，通常可以控制在 1 分钟以内。实际用时会受到网络、模型响应速度、样本数量和仲裁模式影响。

## 5. 如何从零运行项目

本节用于帮助评委老师或第一次接触项目的同学，从一个刚解压的项目包开始，完整跑通环境配置、离线图评估 demo、在线建图评估和结果查看。

### 5.1 创建虚拟环境并安装依赖

进入项目根目录后，先创建 Python 虚拟环境。

Windows PowerShell：

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

`.venv/` 在项目中只保留占位文件，不包含真实环境。这样做是为了让项目结构清晰，同时避免把本地虚拟环境打包进仓库。

### 5.2 设置 LongCat 参数

如果只想查看离线图评估流程，并且关闭二级判断，可以不设置 API Key，直接使用已经保留在 `runs/graphs_offline/` 中的离线图。

如果需要运行在线建图，或者在评估灰区样本时开启 LongCat 二次仲裁，需要设置 LongCat 参数。项目示例默认使用的模型是：

```text
LongCat-Flash-Lite
```

Windows PowerShell：

```powershell
$env:LONGCAT_API_KEY="你的 LongCat API Key"
$env:LONGCAT_BASE_URL="https://api.longcat.chat/openai"
$env:LONGCAT_MODEL="LongCat-Flash-Lite"
```

Linux / macOS：

```bash
export LONGCAT_API_KEY="你的 LongCat API Key"
export LONGCAT_BASE_URL="https://api.longcat.chat/openai"
export LONGCAT_MODEL="LongCat-Flash-Lite"
```

API Key 只应放在本地环境变量或界面输入框中，不要写入代码、配置文件、评估报告或压缩包。

### 5.3 运行离线图评估 demo

离线图评估适合比赛展示和复现实验结果。它不会重新调用 LongCat 建图，而是直接读取项目中已有的 `graph.json`，再对 `data/dialogues` 中的对话样本进行本地评估，并在需要时开启少量二次仲裁。

图形界面运行方式：

```bash
python app_offline.py
```

在界面中选择已有离线图、对话数据目录、正包或负包，并选择二级判断模式。为了稳定复现展示效果，推荐优先使用 `app_offline.py`。

也可以使用命令行快速验证商家包：

```bash
PYTHONPATH=src python scripts/run_offline_graph.py \
  --graph runs/graphs_offline/course_publish_upgrade_v1.json \
  --dialogues data/dialogues \
  --pack all \
  --llm-mode off
```

命令行快速验证骑手包：

```bash
PYTHONPATH=src python scripts/run_offline_graph.py \
  --graph runs/graphs_offline/flyleg_rider_call_v1.json \
  --dialogues data/dialogues \
  --pack all \
  --llm-mode off
```

其中：

- `--graph` 指定要使用的离线状态图；
- `--dialogues` 指定对话数据目录；
- `--pack all` 表示同时评估正包和负包；
- `--llm-mode off` 表示关闭二次仲裁，只查看纯本地评估结果。

如果希望查看 LongCat 二次仲裁效果，可以在图形界面中选择辅助模式，或在命令行中按实际配置打开对应的 LLM 模式。

### 5.4 运行在线建图 + 评估

如果需要展示从复杂指令到状态图再到评估报告的完整链路，可以运行：

```bash
python app.py
```

该模式会执行：

```text
输入复杂客服指令
→ LongCat-Flash-Lite 建图
→ 本地 Schema Linter / Schema Compiler
→ 读取对话数据
→ 本地 Graph Evaluator 评估
→ 可选 LongCat 二次仲裁
→ 生成中文可解释报告
```

在界面中粘贴复杂客服指令，填写 LongCat API Key，选择建图模式、评估数据包和二级判断模式。系统会把运行产物写入类似下面的目录：

```text
runs/longcat_latest__时间戳/
```

在线建图模式会额外统计 LongCat 建图 tokens、建图用时、评估用时和二次仲裁 tokens，因此更适合展示系统完整能力；离线图评估模式则更适合稳定复现已有结果。

### 5.5 查看运行输出

一次运行结束后，重点查看以下文件：

| 文件 | 说明 |
| --- | --- |
| `report.html` | 当前选择的报告入口。 |
| `report_simple.html` | 面向评委的简版结果报告，重点展示总分、通过情况和主要归因。 |
| `report_detail.html` | 面向技术评审的详细过程报告，展示节点、知识、限制、证据和仲裁过程。 |
| `all_reports_merged.json` | 所有样本的结构化评估明细。 |
| `graph.json` | 本次使用或生成的状态图。 |
| `run_manifest.json` | 本次运行索引，记录输入、输出和关键路径。 |
| `run_token_usage.json` | LongCat 调用次数和 token 统计。 |
| `run_timing_summary.json` | LongCat 建图、本地评估、二次仲裁等耗时统计。 |

项目中已经保留了演示报告例子，可以直接打开：

```text
runs/merchant_example/report_detail.html
runs/ridder_example/report_detail.html
```

这两份 example 使用 `LongCat-Flash-Lite` 完成离线建图，并在辅助模式下对局部灰区进行二次仲裁，适合评委直接查看系统的中文可解释报告效果。

## 6. 数据集说明

当前本地数据集主要围绕赛题给出的两类复杂客服指令构建：

1. **飞毛腿骑手合同生效通知与提醒**
2. **商家直播课程发布能力升级告知**

数据集包含正包和负包。正包用于验证系统能否识别流程完整、知识正确、限制合规的客服对话；负包用于验证系统能否识别流程缺失、知识错误、限制违规、异常终止处理不当等问题。

本地数据集目前主要针对上述两大题目组所给指令设计。如需引入其他业务数据，读取数据集的接口和样本组织方式后续可以进一步扩展。由于比赛时间有限，当前版本保留了围绕两类核心任务的设计。详细的介绍可见

## 7. 无硬编码与无泄漏说明

为了给 LLM 建图留下保底机制，本地存在少量通用话术编码，例如“不方便”“稍后”“以规则为准”“不能承诺”等跨业务客服表达。这类内容不绑定具体业务事实，主要用于增强通用语义匹配能力。

但项目拒绝针对具体业务添加业务话术硬编码，例如不把“飞毛腿”“骑手合同”“标准直播”“低延迟直播”“派单”“费用优惠”等具体业务词写在 evaluator 里。业务事实必须来自 LongCat 生成的 graph/schema，而不是由代码提前作弊式写入。

同时，负包中的 `wrong_statement`、`evidence_span` 等标注信息不能被编译成判分答案，避免负包泄漏。项目内自带硬编码检测和负包泄漏自查工具，用于确保泛化性和结果的非作弊性：

```bash
PYTHONPATH=src python tools/hardcode_guard.py
PYTHONPATH=src python tools/anti_leak_guard.py
PYTHONPATH=src python tools/negative_purity_check.py data/dialogues/negative_pack
```

## 8. 详细文档导航

根目录 README 用于总览项目。更详细的说明请查看 `docs/` 目录：

```text
docs/01_PROJECT_STRUCTURE_AND_USAGE.md
docs/02_METHOD_AND_MODULES.md
docs/03_EVALUATION_CASE_WALKTHROUGH.md
docs/04_DATASET_DESIGN.md
```

四份文档分别介绍：

- 项目文件结构与从零使用方式；
- 方法流程与代码模块；
- 一对正负样本如何完成评估的示例；
- 数据集构成、多样性和测试覆盖面。

## 9. 联系方式

如果评委老师或后续使用者有其他问题，欢迎联系：

- 队长邮箱：2939065909@qq.com
- 微信 / 手机号：19518228303

欢迎各位老师提出意见与建议!
