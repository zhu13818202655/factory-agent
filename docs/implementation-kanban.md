# factory-agent 实施看板

## DONE

### 确认采用单 Git 仓库、根主应用与 `mock-mes/` 自包含子项目

### 初始化目标 Git 仓库

### 根 FastAPI 应用、`/health/live`、`/health/ready` 与无网络单元测试

### Mock MES 独立包、相同健康探针与无网络单元测试

### 根/局部 `AGENTS.md`、架构、安全基线和仓库边界 ADR

### uv workspace、单一 `uv.lock`、Makefile、Ruff、Pyright strict 和 pytest 分层

### Story JSON Schema、机器可读 STORY-001 和 PR 模板

### 非 root Dockerfile、分层 Compose 与 GitHub Actions CI 配置

### Bandit、依赖漏洞审计和跨包禁止导入测试

### STORY-001 验证日志（2026-08-17）

  - tags: [STORY-001, verification]
  - defaultExpanded: true
  - steps:
      - [x] `make bootstrap && make check`：通过；11 tests，Pyright 0 errors
      - [x] `make security`：通过；Bandit 通过，依赖 0 个已知漏洞，3 tests
      - [x] `make compose-config`：通过
      - [x] `make build-images && make test-images`：通过；2 镜像，UID 10001，live/ready 正常
      - [x] 独立只读 Reviewer：通过；0 个未解决高/中 finding

## DOING

### 当前里程碑：Phase 0：仓库地基

  - tags: [Phase 0, milestone]
  - defaultExpanded: true
    ```md
    更新日期：2026-08-17
    退出条件：新环境能够执行 `make bootstrap && make check`，根应用与 Mock MES 均能独立启动、测试和构建，且不存在生产代码对 Mock 的导入依赖。
    ```

### STORY-001：工程地基（实现完成，状态：review）

  - tags: [STORY-001, Phase 0, review]
  - defaultExpanded: true
  - steps:
      - [x] 代码、文档、门禁和 Compose 配置
      - [x] 两个容器镜像完成构建与运行时健康验证
      - [x] 独立只读 Reviewer 无高/中 finding
      - [ ] 在具备 Snyk Code 的环境补扫后转为 `done`

## TODO

### STORY-002：Canonical OpenAPI

  - steps:
      - [ ] 定义身份、组织、员工、订单、工序、计件、计划和结算资源
      - [ ] 固定分页、批量 ID、错误和版本兼容规则

### STORY-003：Mock PostgreSQL 与确定性 seed

  - steps:
      - [ ] SQLAlchemy 模型和 Alembic 基线
      - [ ] `small`、`standard`、`load` 场景及虚拟时钟

### STORY-004：Mock 资源 API

  - steps:
      - [ ] 实现 Canonical consumer contract
      - [ ] 增加分页一致性、空值和边界数据测试

### STORY-005：故障注入

  - steps:
      - [ ] 延迟、429、5xx、分页异常和字段漂移

### STORY-006：LLM Gateway 测试设施

  - steps:
      - [ ] 进程内 Fake、pytest 临时 upstream 和 LiteLLM harness

### STORY-007：vLLM 基准与部署

### STORY-008：LiteLLM 有序 fallback 集成测试

### STORY-009~016：Identity、DataScope、权限、DAG、DuckDB、L1 与 L2

### 后续实施顺序

  - tags: [roadmap]
  - defaultExpanded: true
  - steps:
      - [x] 完成 STORY-001，让所有后续 Story 都有统一、可执行的门禁
      - [ ] 完成 STORY-002~005，用固定 seed 建立无客户环境也可验收的 API 闭环
      - [ ] 完成 STORY-006~008，验证本地模型优先和远端有序降级
      - [ ] 以“小组产量对比”为第一条业务纵向切片，实现权限、三 API、DuckDB 和导出
      - [ ] 批量完成其余 L1 能力；L1 稳定后才开放 L2 规划

## BLOCKED

### Snyk Code 工具和 CLI 在当前环境均不可用；合并前需在提供 Snyk 的 CI/IDE 环境补扫

### `CODEOWNERS` 需要仓库所属 GitHub 组织和 team/用户名，当前不能填写虚假 owner

### 客户真实 OpenAPI 与脱敏响应样例尚未提供；不阻塞 Canonical/Mock 开发

### 35B MoE 的具体模型和量化格式待 benchmark 后决定

### 远端主备模型供应商及预算待 fallback 联调前确认

### 工资、计划达成率和订单进度最终业务口径待客户确认
