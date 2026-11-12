# ASES 控制舱前端镜像
#
# 前后端分离之后，前端是一个**独立的静态资源**。这个镜像干两件事：
#   1. 构建阶段用 Node 把 TypeScript/React 编译成静态文件；
#   2. 运行阶段用 nginx 把静态文件发出去，并把 /api 反代给后端。
#
# **为什么运行阶段是 nginx 而不是 Node。**
# 构建产物是纯静态文件（HTML + JS + CSS），不需要 Node 运行时 ——
# 用 nginx 意味着运行镜像里没有 Node，也没有 node_modules，
# 攻击面小得多，冷启动也快得多（毫秒级 vs 秒级）。
#
# **为什么 nginx 要反代 /api。**
# 反代之后浏览器看到的只有 nginx 一个来源，控制舱与 API 同源 ——
# 于是**完全不需要 CORS**。这比"前端独立域名 + 配 CORS 白名单"少一整类
# 故障：白名单配错时，浏览器会拦掉响应，而 curl 完全正常，
# 于是"后端没问题"这句话会被反复重复，直到有人去翻浏览器控制台。
# 真的要把前端放到独立域名时，再在 nginx 里把 /api 反代到那个域名，
# 或者打开后端的 CORS 白名单（ASES_CORS_ALLOWED_ORIGINS）。

# 基础镜像做成参数，是为了能指向内部镜像仓库。
ARG NODE_IMAGE=node:22-alpine
ARG NGINX_IMAGE=nginx:1.27-alpine

# ---------------------------------------------------------------------------
# 构建阶段
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS build

WORKDIR /app

# 先只拷贝依赖清单，让依赖层可以被缓存。
# 改一行业务代码不该让 npm 重新下载一遍依赖。
COPY package.json package-lock.json ./

# npm ci：严格按 lock 文件装，版本可复现 —— 与 CI、本地开发一致。
RUN npm ci

COPY tsconfig.json tsconfig.node.json vite.config.ts index.html ./
COPY postcss.config.js tailwind.config.js ./
COPY src ./src

# 构建。**类型检查在这里跑**（package.json 的 build 脚本先 typecheck
# 再 vite build）—— 类型错误不许进镜像。类型错误进了镜像，症状是
# "某个数字莫名其妙变成 —"，排查起来极其痛苦。
#
# VITE_API_BASE_URL 不传：默认走相对路径 /api/v1，由 nginx 反代到后端，
# 前后端同源。写死绝对地址会把镜像绑死在某个域名上。
RUN npm run build

# ---------------------------------------------------------------------------
# 运行阶段
# ---------------------------------------------------------------------------
FROM ${NGINX_IMAGE} AS runtime

# 后端地址。**这是部署时可改的** —— 容器启动时 nginx 官方镜像的
# entrypoint 会把 /etc/nginx/templates/*.template 里的 ${API_UPSTREAM}
# 换成这个环境变量的值。用 compose 时可以改这个值，不必重新构建镜像。
ENV API_UPSTREAM="api:8000"

# 自己的 nginx 配置替换掉默认的。默认配置只服务于"给个静态目录"，
# 这里要的是"发静态文件 + 反代 /api + SPA 回退"。
#
# **放进 templates/ 而不是直接放 conf.d/** —— 模板会在启动时被渲染
# （${API_UPSTREAM} 换成环境变量），渲染结果落到 /etc/nginx/conf.d/。
#
# 官方镜像自带的 default.conf **必须删掉**：它也是一个 listen 80 的
# server，两份配置同时存在时请求会落到**先注册的那一份** ——
# 也就是官方那个只会回一句 "Welcome to nginx!" 的默认页。
# nginx 只对重复的 server_name 报一句 warning，**不会拒绝启动**，
# 于是症状是"部署成功了，但打开是个 nginx 欢迎页"。
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf.template /etc/nginx/templates/nginx.conf.template

# 安全响应头片段放在 snippets/ 而不是 conf.d/ ——
# nginx 会把 conf.d/*.conf 整个 include 进 http 层级，
# 而 ``add_header`` 在 http 层级是**非法指令**（会导致 nginx 起不来）。
COPY security-headers.conf /etc/nginx/snippets/security-headers.conf

COPY --from=build /app/dist /usr/share/nginx/html

# nginx 官方镜像里，nginx 以 root 启动（要绑 80 端口），
# 但 worker 进程跑在非 root 用户上。这里保持官方做法 ——
# 端口由 compose 映射到宿主机高位端口，容器内部仍是 80。
EXPOSE 80

# 容器内的健康检查由 compose 定义（见 docker-compose.yml）。

CMD ["nginx", "-g", "daemon off;"]
