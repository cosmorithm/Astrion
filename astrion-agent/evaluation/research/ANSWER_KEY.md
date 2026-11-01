# Astrion Research Eval v1：标准要点草案

**这些要点由项目准备过程依据原文提取编写；尚未全部由人工独立复核，不能当作已确认金标准。**
页码为 PDF 文件物理页码，从 1 开始。锚点是提取文本中的短句；格式或断行可能不同，请回看原始页面。

## R01 · dev

比较论文 P01、P02、P03 的综述关注点和主要组织方式，各指出一个可由原文支持的差别。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C01-P1**：P01 对多智能体中的环境、角色、通信和能力等方面作综述。
  - 依据：`01_2402.01680_llm-multi-agents-survey.pdf`，PDF 第 1 页；锚点：`How are these agents`
- **C01-P2**：P02 聚焦多智能体协作机制，并给出可扩展的分类框架。
  - 依据：`02_2501.06322_collaboration-mechanisms-survey.pdf`，PDF 第 1 页；锚点：`extensible framework to guide future research`
- **C01-P3**：P03 将协作、故障归因和自演化联系起来。
  - 依据：`03_2605.14892_life-survey.pdf`，PDF 第 1 页；锚点：`causal dependencies`

## R02 · holdout

比较 P04、P05、P06 的综述主题和分类重点。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C02-P1**：P04 以多智能体通信的五个 W 组织。
  - 依据：`04_2602.11583_five-ws-communication.pdf`，PDF 第 1 页；锚点：`Five Ws of communica-`
- **C02-P2**：P05 聚焦多智能体辩论，分类包含参与者、交互与一致协议。
  - 依据：`05_2607.26212_multi-agent-debate-survey.pdf`，PDF 第 1 页；锚点：`three-dimensional taxonomy covering debate participants`
- **C02-P3**：P06 聚焦多智能体系统的创造力。
  - 依据：`06_2505.21116_creativity-mas-survey.pdf`，PDF 第 1 页；锚点：`creativity in MAS`

## R03 · dev

比较 P07、P08、P09 分别采用何种方式组织或定义多智能体协作。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C03-P1**：P07 使用角色扮演与 inception prompting。
  - 依据：`07_2303.17760_camel.pdf`，PDF 第 1 页；锚点：`role- playing`
- **C03-P2**：P08 将标准操作流程编码为提示序列。
  - 依据：`08_2308.00352_metagpt.pdf`，PDF 第 1 页；锚点：`Standardized Operating Procedures`
- **C03-P3**：P09 使用可定制、可对话的多智能体，并可结合工具与人。
  - 依据：`09_2308.08155_autogen.pdf`，PDF 第 1 页；锚点：`converse with each other`

## R04 · holdout

比较 P10、P11、P12 的系统目标以及协作方式。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C04-P1**：P10 面向软件开发，以 chat chain 和 communicative dehallucination 组织交流。
  - 依据：`10_2307.07924_chatdev.pdf`，PDF 第 1 页；锚点：`chat chain`
- **C04-P2**：P11 组织专家智能体组以完成任务。
  - 依据：`11_2308.10848_agentverse.pdf`，PDF 第 1 页；锚点：`collaborative group of expert agents`
- **C04-P3**：P12 通过多个模型实例多轮提出和辩论答案。
  - 依据：`12_2305.14325_multiagent-debate.pdf`，PDF 第 1 页；锚点：`multiple language model instances propose`

## R05 · dev

比较 P13、P14、P15 如何使用反馈或记忆支持多智能体适应。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C05-P1**：P13 用原子结构编辑和单次编辑反事实比较改进协作结构。
  - 依据：`13_2610.30429_roleflow.pdf`，PDF 第 1 页；锚点：`Single-Edit Counterfactual Pol-`
- **C05-P2**：P14 把技能、记忆与协作结合，并使用反事实轨迹。
  - 依据：`14_2610.33539_coevolve.pdf`，PDF 第 1 页；锚点：`counterfactual trajectories`
- **C05-P3**：P15 使用每个主智能体对应的 MemoryAgent 支持经验驱动适应。
  - 依据：`15_2610.34450_evolvemas.pdf`，PDF 第 1 页；锚点：`dedicated MemoryAgent`

## R06 · holdout

比较 P16、P17、P18 分别从什么角度改善多智能体协作。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C06-P1**：P16 使用信念条件化的多智能体 actor-critic。
  - 依据：`16_2610.35398_bcmaac.pdf`，PDF 第 1 页；锚点：`Belief-Conditioned`
- **C06-P2**：P17 研究多智能体配置路由，使用 insight-guided on-policy distillation。
  - 依据：`17_2610.35690_evorouter.pdf`，PDF 第 1 页；锚点：`On-Policy Distillation`
- **C06-P3**：P18 通过剪枝冗余或恶意通信消息降低开销。
  - 依据：`18_2410.02506_agentprune.pdf`，PDF 第 1 页；锚点：`prunes redundant`

## R07 · dev

P19 使用怎样的图结构组织多智能体？它报告可以支持何种规模？ 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C07-P1**：MACNET 用有向无环图组织智能体，摘要报告超过一千个智能体。
  - 依据：`19_2406.07155_macnet.pdf`，PDF 第 1 页；锚点：`directed acyclic graphs`
- **C07-P2**：报告支持超过一千个智能体。
  - 依据：`19_2406.07155_macnet.pdf`，PDF 第 1 页；锚点：`over a thousand agents`

## R08 · dev

P20 的 MAST 将失效模式归为哪三个类别？ 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C08-P1**：三类是系统设计问题、智能体间错位、任务验证。
  - 依据：`20_2503.13657_why-mas-fail.pdf`，PDF 第 1 页；锚点：`system design issues`

## R09 · holdout

P21 基于多少失败轨迹构建通信失败分类，并指出两种主要失败维度？ 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C09-P1**：基于 3211 条失败多轮轨迹；主要问题包括恢复/纠错和共识/决策。
  - 依据：`21_2610.32060_communication-failures.pdf`，PDF 第 1 页；锚点：`3,211 failed`
- **C09-P2**：主要问题涉及恢复/纠错和共识/决策。
  - 依据：`21_2610.32060_communication-failures.pdf`，PDF 第 1 页；锚点：`recovery/correction and consensus/decision`

## R10 · holdout

P22 的工作流研究使用哪些方法产生并挖掘工作流？ 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C10-P1**：用 AFlow 的 MCTS 搜索产生工作流，随后用沙盒 AutoResearch 挖掘。
  - 依据：`22_2610.31709_workflow-design-space.pdf`，PDF 第 1 页；锚点：`MCTS optimizer`
- **C10-P2**：使用 sandboxed AutoResearch loop。
  - 依据：`22_2610.31709_workflow-design-space.pdf`，PDF 第 1 页；锚点：`sandboxed AutoResearch loop`

## R11 · holdout

P24 的摘要比较了多少种系统、模型和临床任务？分别说明含义，不要混淆。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C11-P1**：四种系统：一个单模型和三个多智能体拓扑。
  - 依据：`24_2610.27027_hallucination-clinical.pdf`，PDF 第 1 页；锚点：`four systems`
- **C11-P2**：四个模型与三个临床任务。
  - 依据：`24_2610.27027_hallucination-clinical.pdf`，PDF 第 1 页；锚点：`four models and three clin-`

## R12 · dev

P25 在 BigCodeBench 上报告多少准确率百分点变化和部署 token 变化？请说明比较基准，不要混淆百分点与百分比。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C12-P1**：相对 score-only heuristic search，平均准确率提升 5.3 个百分点，部署 token 减少 54%。
  - 依据：`25_2610.29292_credit-attribution-topology.pdf`，PDF 第 1 页；锚点：`5.3 percentage points`
- **C12-P2**：部署 token 减少 54%。
  - 依据：`25_2610.29292_credit-attribution-topology.pdf`，PDF 第 1 页；锚点：`54% fewer deployment tokens`

## R13 · holdout

P05 综述了多少项关于多智能体辩论的初级研究？分类的三个维度是什么？ 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C13-P1**：系统综述 141 项研究；维度是参与者、交互机制和达成一致的协议。
  - 依据：`05_2607.26212_multi-agent-debate-survey.pdf`，PDF 第 1 页；锚点：`141 primary studies`
  - 依据：`05_2607.26212_multi-agent-debate-survey.pdf`，PDF 第 1 页；锚点：`agreement protocols`

## R14 · dev

P13 摘要中给出多少基准、多少领域，以及相对最强对手的平均提升？ 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C14-P1**：七个基准、四个领域、超过最强对手 4.7 分。
  - 依据：`13_2610.30429_roleflow.pdf`，PDF 第 1 页；锚点：`seven benchmarks spanning four domains`
  - 依据：`13_2610.30429_roleflow.pdf`，PDF 第 1 页；锚点：`strongest competitor by 4.7 points`

## R15 · dev

仅依据 P23 的 PDF 第 1 页，能否列出试验中每一首歌的歌名和逐首推荐次数？说明依据与不足。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C15-P1**：该页描述音乐推荐与累计推荐数实验，但不能给出逐首歌名和逐首次数；应说明证据不足。
  - 依据：`23_2610.30769_herding-cascades.pdf`，PDF 第 1 页；锚点：`unfamiliar songs`

## R16 · holdout

仅依据 P24 的 PDF 第 1 页，能否给出每位受试患者的姓名及具体诊疗结果？说明原文能支持什么。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C16-P1**：该页描述四个系统、四个模型和三个临床任务，并未提供逐位患者姓名及具体结果。
  - 依据：`24_2610.27027_hallucination-clinical.pdf`，PDF 第 1 页；锚点：`a single-LLM baseline`

## R17 · holdout

仅依据 P21 的 PDF 第 1 页，能否给出每个数据集中每一种失败类型的精确次数？说明有哪些信息、缺少哪些信息。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C17-P1**：该页提供 3211 条失败轨迹及领域、分类研究概述；不能列出逐数据集逐类型精确次数。
  - 依据：`21_2610.32060_communication-failures.pdf`，PDF 第 1 页；锚点：`3,211 failed`

## R18 · dev

阅读 P01 的 PDF 第 1、2 页：概括这项综述提出的主要问题，以及第 2 页描述的四个分析方面。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C18-P1**：讨论多智能体环境、角色、通信、能力等问题。
  - 依据：`01_2402.01680_llm-multi-agents-survey.pdf`，PDF 第 1 页；锚点：`How are these agents`
- **C18-P2**：第二页列出智能体—环境接口、角色画像、通信、能力获取。
  - 依据：`01_2402.01680_llm-multi-agents-survey.pdf`，PDF 第 2 页；锚点：`agent capability acquisition`

## R19 · holdout

阅读 P18 的 PDF 第 1、2 页：它试图解决什么通信问题？文中如何区分 intra-dialogue 与 inter-dialogue？ 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C19-P1**：减少多智能体通信的冗余和成本。
  - 依据：`18_2410.02506_agentprune.pdf`，PDF 第 1 页；锚点：`prunes redundant`
- **C19-P2**：intra-dialogue 为同一轮内部智能体互动；inter-dialogue 为跨轮传递内容。
  - 依据：`18_2410.02506_agentprune.pdf`，PDF 第 2 页；锚点：`inter-dialogue communication`
  - 依据：`18_2410.02506_agentprune.pdf`，PDF 第 2 页；锚点：`Intra-dialogue communication:`
  - 依据：`18_2410.02506_agentprune.pdf`，PDF 第 2 页；锚点：`Inter-dialogue communication:`

## R20 · dev

阅读 P25 的 PDF 第 1、2 页：概括 CATS 目标，以及方法如何对候选团队的 agent 归因。 请使用项目资料并引用可定位的原文；证据不足时说明。

- **C20-P1**：CATS 在固定 supergraph 中寻找紧凑团队。
  - 依据：`25_2610.29292_credit-attribution-topology.pdf`，PDF 第 1 页；锚点：`fixed supergraph`
- **C20-P2**：LLM attributer 比较同一任务的候选运行并赋予 agent 贡献分数。
  - 依据：`25_2610.29292_credit-attribution-topology.pdf`，PDF 第 2 页；锚点：`Credit`
  - 依据：`25_2610.29292_credit-attribution-topology.pdf`，PDF 第 2 页；锚点：`Attribution.An LLM attributer`
