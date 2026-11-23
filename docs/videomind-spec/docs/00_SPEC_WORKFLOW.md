# 规范驱动开发流程

本规范包用于把已讨论的VideoMind目标交给Codex实施。它提供行为要求、技术约束、接口合同、任务和发布标准。当前交付状态是规范完成；应用尚需在目标仓库中开发和验证。

## 1 规范与实现的关系

PRD回答谁在什么情境下需要什么能力；feature spec规定用户可观察行为与边界；plan规定数据与技术实现；contracts固定跨模块接口；tasks拆成可审查改动；验收矩阵说明怎样证明目标实现。所有层次用REQ、T、AT编号串联，避免产品描述与代码各说各话。

本文档采用Spec Kit所体现的constitution、specification、plan、tasks、implementation、convergence组织方式，并加入已有仓库审计与发布证据。官方工具的命令和集成形式随版本变化；本包可以直接由Codex读文件执行，不依赖预先安装Spec Kit。[S01]

## 2 目录与阅读顺序

1. README.md：交付范围、入口和使用顺序。
2. .specify/memory/constitution.md：开发原则。
3. docs/01_PRD.md与02_UX_FLOWS.md：用户任务、产品行为和页面状态。
4. docs/03_ARCHITECTURE.md至06_DATA_API.md：技术边界、媒体与检索、Harness与编辑、统一数据合同。
5. contracts：OpenAPI、JSON Schema、工具注册和正反例。
6. specs中本次阶段相关的spec、plan、tasks；无需每轮塞入全部文档。
7. docs/07_CODEX_EXECUTION.md与08_VERIFICATION_RELEASE.md：执行协议和验收。
8. docs/09_DECISIONS_AND_SOURCES.md、traceability、status：决策依据、可追踪关系和当前进度。

## 3 阶段和关口

| 阶段 | 输入 | 必须产出的结果 | 进入下一阶段的条件 |
|---|---|---|---|
| 仓库审计 | 当前checkout与本规范 | baseline report、现有能力与差异清单、依赖和配置状态 | 找到真实入口与测试方法，保留用户改动 |
| 明确需求 | PRD、用户已有偏好 | 已决策/假设/阻塞问题列表 | 只保留会改变当前实现且无法合理默认的阻塞 |
| 细化规格 | feature spec | Given/When/Then与异常行为 | 必要行为均能映射到验收 |
| 技术计划 | spec、现有代码、contracts | 文件级变更计划、迁移、失败语义 | 没有未解决的合同冲突 |
| 拆解任务 | plan、依赖关系 | 待办task、验收命令与负责人 | 首个任务可以独立实施 |
| 实现 | 一个有界task | 代码、测试、必要迁移、文档同步 | 实际执行对应验收且解释失败 |
| 收敛 | 实现差异、AT结果 | review findings与修复、状态更新 | spec与实际行为一致，关联AT通过 |
| 发布 | 全阶段证据 | release report、部署和恢复记录、演示 | M6所有强制关口通过；否则列明未通过项 |

关口用于控制开发质量，不要求每完成一个普通可逆步骤就向用户重复申请许可。Codex在已有授权内持续工作，遇到真实外部依赖或必须由用户决定的产品冲突时才说明阻塞。

## 4 里程碑顺序

M0确认仓库、模型合同、鉴权和验证基础。M1先完成显式区间剪辑的真实闭环。M2完成长视频分块和多模态记忆。M3完成检索与证据问答。M4把语义选片接到编辑闭环。M5引入可恢复的Agent Team。M6完成发布验收。

feature编号按领域划分而非执行先后。因此005的基础编辑任务会在001和002的完整智能处理之前执行。不要机械按目录001到006逐个全部完成，否则第一条可见剪辑链会被推迟。

## 5 什么信息允许默认

沿用现有语言、框架、认证、数据存储、包管理与CI属于默认；参数初值和未承诺的性能预算可以按本规范配置并记录ADR。用户已经决定通用模型使用Flash，因此不再次询问是否改用Pro。

无法确认目标仓库、缺少模型凭证、现有数据迁移会不可逆破坏、对外发布目标不明确，属于可能的阻塞。先完成不受阻塞影响的合同、实现和离线验证，再准确列出剩余依赖。密钥通过环境或秘密管理注入，不放进聊天、日志、测试fixture和提交。

## 6 Spec到代码的追踪

requirements.json保存需求；task-index.json保存任务定义、依赖和对应REQ；acceptance-matrix.json保存AT和验证方法。一个需求可对应多个任务和多个AT，不能只凭一个截图或README声明验收。状态文件记录运行证据路径，初始均为TODO或NOT_RUN。

新增范围先新增REQ并更新spec、contracts和AT；修复不改变预期行为时，引用已有REQ和失败AT。任务超出可审查范围时拆分子任务，不把隐藏需求塞进实现。

## 7 状态定义

| 状态 | 含义 |
|---|---|
| TODO | 尚未实施 |
| IN_PROGRESS | 当前正在实施，有明确边界 |
| IMPLEMENTED | 有代码，尚未完成全部验收 |
| VERIFIED | 对应验收已实际执行并通过 |
| BLOCKED | 存在具体外部依赖或不可自行决定的问题 |
| FAILED | 已执行验收但结果不满足规范 |

“可以运行单元测试”“已有mock”“模型接口能返回文本”均不等于最终功能已验证。release状态单独维护，任何强制AT未跑或失败都不能标为RELEASE_READY。

## 8 每次交接保留什么

status/PROGRESS.md包含本轮目标、完成任务、未完成任务、失败与阻塞、执行命令和结果、下一个具体任务。长日志、图片和视频放evidence目录，用ID或路径引用。上下文压缩时保留这些状态，不复制整段对话作为唯一记忆。

## 9 参考

[S01] GitHub Spec Kit官方README与SDD方法：https://github.com/github/spec-kit 和 https://github.com/github/spec-kit/blob/main/spec-driven.md 。本包采用其流程思想，未假定目标环境已经安装其命令或扩展。
