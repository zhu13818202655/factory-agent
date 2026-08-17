# EPIC-000: 可验证工程地基

## 目标

建立根产品应用和 Mock MES 的可复现工程闭环，使后续每个 Story 都能通过统一命令获得
机器可判定的验收结果。

## 范围

- 根应用与 Mock MES 独立启动、测试和构建。
- 单 Git、单 uv lock、明确 HTTP 契约边界。
- 分层 AGENTS、Story Schema、CI、静态检查、安全扫描和容器基线。

## 退出条件

`make bootstrap && make check` 通过，两个镜像能够以非 root 用户运行并返回健康探针，且
独立 Reviewer 没有未解决的高/中风险 finding。
