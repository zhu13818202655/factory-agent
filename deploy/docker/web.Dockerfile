# ============================================================================
# mes-ai-assistant 统一站点镜像：Web 助手（frontend）+ H5 助手（frontend-h5）
# 由 Caddy 托管。两种运行形态由环境变量 WEB_PROFILE 选择（compose profile 对应）：
#   domain      —— 自管域名 hz.jundadt.com.cn，alidns DNS-01 自动签发
#                 （Caddyfile.domain，需 ALIYUN_ACCESS_KEY_ID / _SECRET）
#   srv-network —— 接入外部 docker 网络 srv-domain，由既有反向代理终结 TLS，
#                 本容器只出干净 HTTP :80（Caddyfile.srv-network）
#
# 镜像加速（国内源）：
#   REGISTRY     —— Docker 镜像仓库前缀，默认 gh-proxy.org/docker（可匿名直连）；
#                   回退官方源请显式 --build-arg REGISTRY=docker.io，不要留空
#   NPM_REGISTRY —— npm 源，默认 https://registry.npmmirror.com
#
# 构建（context 必须是仓库根目录，两个前端子模块都要 COPY）：
#   docker build -f deploy/docker/web.Dockerfile -t mes-ai-assistant:latest .
# ============================================================================
ARG REGISTRY=gh-proxy.org/docker
ARG NPM_REGISTRY=https://registry.npmmirror.com

# 阶段 1：编译带 alidns 插件的 Caddy（Go 模块走 goproxy.cn 加速）
FROM ${REGISTRY}/library/caddy:2.11.4-builder-alpine AS caddy-builder
ENV GOPROXY=https://goproxy.cn,direct
RUN xcaddy build \
    --with github.com/caddy-dns/alidns

# 阶段 2：构建 Web 助手（Vue3 + vite；build:prod = --mode production）
# 真实 .env.* 已被两个前端项目 gitignore，构建期统一从 .env.example 复制
# .env.production。frontend 的三档默认值（demo/local-keys/apiBase）内建在
# vite.config.ts 的 MODE_DEFAULTS（生产 apiBase 置空 = 同源 /v1/），副本与之保持一致。
FROM ${REGISTRY}/library/node:22-alpine AS web-builder
ARG NPM_REGISTRY
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm config set registry ${NPM_REGISTRY} \
    && npm ci --no-audit --no-fund
COPY frontend/ ./
RUN cp .env.example .env.production \
    && npm run build:prod

# 阶段 3：构建 H5 助手（Vue3 + vite；npm run build 默认 --mode production）
# 生产 apiBase 置空：前端请求走同源 /v1/...，由 Caddy 原样透传到后端
#（与 Web 助手同一约定，.env.example 模板里 VITE_API_BASE_URL 已留空）。
FROM ${REGISTRY}/library/node:22-alpine AS h5-builder
ARG NPM_REGISTRY
WORKDIR /app
COPY frontend-h5/package.json frontend-h5/package-lock.json ./
RUN npm config set registry ${NPM_REGISTRY} \
    && npm ci --no-audit --no-fund
COPY frontend-h5/ ./
RUN cp .env.example .env.production \
    && npm run build

# 阶段 4：运行时（官方 caddy alpine + 自编译 alidns 版 caddy + 两个前端产物）
FROM ${REGISTRY}/library/caddy:2.11.4-alpine
COPY --from=caddy-builder /usr/bin/caddy /usr/bin/caddy
COPY --from=web-builder /app/dist /srv
COPY --from=h5-builder /app/dist /srv/h5
COPY deploy/docker/Caddyfile.domain /etc/caddy/Caddyfile.domain
COPY deploy/docker/Caddyfile.srv-network /etc/caddy/Caddyfile.srv-network
ENV WEB_PROFILE=domain
EXPOSE 80 443
# caddy 官方镜像无 ENTRYPOINT，CMD 直接覆盖即可；按 WEB_PROFILE 选 Caddyfile
CMD ["sh", "-c", "caddy run --config /etc/caddy/Caddyfile.${WEB_PROFILE:-domain} --adapter caddyfile"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
