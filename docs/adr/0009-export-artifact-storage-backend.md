# ADR-0009：导出产物的存储后端（本地目录 ↔ S3 兼容对象存储）

- 状态：Accepted（2026-09-14 实现；阶段 1~3 + T3.11）
- 日期：2026-09-14
- 所有者：项目维护者
- 关联：`docs/design/链路优化方案.md`（阶段 1~3、T3.11）、
  `src/factory_agent/ports/artifacts.py`、`src/factory_agent/export/`、
  `src/factory_agent/statistics/export_store.py`、
  `docs/product/需求及方案整理.md`（报表导出与文件留存策略）、
  ADR-0001（服务边界）、ADR-0002（存储基线）、ADR-0003（表归属）

## 1. 背景（现状，已验证）

导出产物原先只写本地目录。两个问题：

1. `agent-api` 容器以 `read_only: true` 运行，本地目录必须靠可写卷才能落盘，扩副本时
   每个副本各写各的本地盘，产物无法跨实例读取。
2. 平台报表导出走 `ExportFileStore` 协议，但唯一实现是
   **纯内存** `InMemoryExportFileStore`——进程重启即丢，只适合 dev/test。

同时，客户口径已统一为「即时生成、**落盘保留**」，默认保留期 90 天
（`需求及方案整理.md` 报表导出与文件留存策略），因此需要一个**跨重启存活**的后端。

## 2. 决策

### 2.1 存储与访问控制分层

- 后端只做「按 id 存 / 取 / 删」，**不做**保留期判定、归属判定、审计。
- 保留期、归属、下载鉴权全部留在服务层（`export_service.py` / `ExportService`）。
  因此换后端不改变「谁能下载什么」。

### 2.2 factory-agent：`ExportStore` 协议 + 两个实现

- 协议在 `ports/artifacts.py`（命名为 `ExportStore`，与 `ports/contracts.py` 的遗留瞬态
  缓冲 `ArtifactStore` 区分）。
- `LocalArtifactStore`（`export/local_store.py`）：默认与测试后端；原子写（临时文件 +
  rename），避免下载读到半写文件。
- `S3ArtifactStore`（`export/s3_store.py`）：生产形态，`aioboto3` + 路径风格寻址。
- 对象布局：`exports/<artifact_id>.xlsx` + 同前缀 `<artifact_id>.json` **sidecar**
  （owner / 展示文件名 / content_type / expires_at）。用 sidecar 而不是 S3 头字段，
  避免中文文件名在 HTTP 头里的编码问题。
- 后端选择：`FACTORY_AGENT_S3_ENDPOINT_URL` 非空 → S3，否则本地目录（`EXPORT_STORE_DIR`）。
  `readiness["export"]` 由二值升级为**后端名**（`s3` / `local` / `fake`）。

### 2.3 下载保持「后端代理」，明确不引入 presigned URL

下载仍走 `api/exports.py`：先按 owner 复核 → 写审计事件 → 审计成功才流式返回字节；
**审计失败直接 503、不放数据**（fail-closed，DEC-014）。presigned URL 会把
「审计失败即不放数据」降级为「URL 一旦签发就是 bearer 令牌」，本次不引入。
该项列为后续可选项，需要时单独拍板。

### 2.4 错误映射

| 情形 | 行为 |
|---|---|
| 写失败（不可达 / 凭据错误 / 客户端异常 / 目录不可写） | `ExportError(UNAVAILABLE)` → 导出被拒，**绝不发一个永远下不到的 id** |
| 读失败 / 对象缺失 / sidecar 损坏 / 字节缺失 | 返回 `None` → 404，与「缺失 / 过期 / 他人」不可区分 |
| 删除失败 | 吞掉并记 warning（best-effort） |

### 2.5 保留期与清理

保持**惰性清理**（不访问就不过期删除），默认 90 天。S3 生命周期规则**不引入**：
它会把清理语义从「服务层可解释」搬到「桶配置里不可见」，与 §2.1 的分层相悖。

### 2.6 平台统计报表：同一协议、独立实现与独立桶

- `src/factory_agent/statistics/export_store.py` 提供 `LocalExportFileStore` /
  `S3ExportFileStore` / `InMemoryExportFileStore`。
- 选择顺序：`FACTORY_AGENT_STATISTICS_S3_ENDPOINT_URL` 非空 → S3；否则
  `FACTORY_AGENT_STATISTICS_EXPORT_STORE_DIR` → 本地目录；两者都空 → 内存
  （dev/test，启动时打 warning）。
- 统计报表保留**签名短链 + 后端代理**的下载语义，与 §2.3 的结论一致。
- 桶独立（`factory-agent-statistics-exports`），与业务 artifact 的
  `factory-agent-exports` 分开：两类产物的配置与保留策略不同，混放会让一方的清理动作碰到
  另一方的对象，配置键也会互相覆盖。

### 2.7 参考后端：单节点 SeaweedFS

`weed mini` 单进程内含 Master + Volume + Filer + S3 网关，官方定位可用于单节点生产。
桶与凭据由环境变量在首次启动时**幂等**播种；`S3_BUCKET` 支持**逗号分隔多个桶**
（`factory-agent-exports,factory-agent-statistics-exports`，已在本机实测:两个桶都被创建）。

## 3. 不变式核对

| 不变式 | 结论 |
|---|---|
| 敏感字段不进 prompt/日志/快照 | 成立：sidecar 只存 owner 标识与展示文件名，不存业务行；下载审计只记 artifact id + scope 摘要 |
| 访问控制不因换后端而放宽 | 成立：owner 复核与审计门仍在服务层/API 层；后端无鉴权语义 |
| 业务回答不受对象存储影响 | 成立：导出是业务提交**之后**的独立步骤，S3 不可用只降级导出（fail-closed），不阻塞问答 |
| 服务边界 | 成立：统计面仍从不调用 MES，也不被业务域 import；两者只共享数据库与同一个对象存储网关，配置前缀与桶各自独立 |

## 4. 后果与风险

- 正面：导出跨实例、跨重启可读；扩副本不再依赖本地卷；平台报表导出不再是
  重启即丢的临时数据。
- 风险一（桶名写错不会报错）：**SeaweedFS 对不存在的桶会自动创建**，PUT 静默成功。
  所以桶名不一致的后果是「产物落进另一个桶」，而不是 fail-closed。真正会触发 fail-closed
  的只有：网关不可达、凭据错误、客户端异常。若要防桶名写错，只能加一道启动/就绪期
  `HeadBucket` 校验（**本次未做，列为可选项**）。
- 风险二（单点无冗余）：`seaweedfs-data` 是唯一副本，必须纳入备份。当前存的是保留期
  90 天的导出产物，丢可重算（重新发起查询即可）；不要把不可再生数据放进这些桶。
- 风险三（凭据入口）：S3 凭据只走 env（`.env` gitignored / compose env），不写入受审
  配置文件；端口只绑回环，不给公网——桶里是工资类数据。
- 回退：置空 S3 端点即回退本地目录后端，无需回滚代码；本地目录仍是默认与测试路径。
