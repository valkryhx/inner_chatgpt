# nginx 入口镜像：**自己构建前端**，静态文件由 nginx 直接返回。
#
# ## 为什么要单独构建，而不是把 dist 打进 app 镜像
#
# 前端是纯静态资源（HTML + JS + CSS），3 个 app 实例各放一份是纯冗余。
# 更要紧的是**路由归属**：
#
#   静态文件 -> nginx 直接返回（不碰 app）
#   /api     -> proxy_pass + 粘性 hash（这才是真正需要粘性的部分）
#
# 如果 dist 打进 app 镜像，`location /` 会统一走 `hash $cookie_hlz_sticky`，
# 于是**页面本身**也被粘性路由 —— 而首次访问还没有 cookie（实测：连发 3 次
# 不带 cookie 的请求会落到 3 个不同实例），静态文件就白占 app 的连接和带宽。
#
# ## 为什么用 npm ci 而不是 npm install
#
# 有 package-lock.json。`ci` 严格按 lock 装，构建可复现；`install` 可能
# 偷偷升级 minor 版本，某天构建突然失败或产物变化。
#
# ## 两个阶段
#
# build 阶段用 node:20-slim（比 alpine 稳，不依赖 glibc 兼容层），
# 只装 devDependencies + 构建。最终镜像只 COPY dist，node 不进生产镜像。

# ---------------------------------------------------------------- 构建阶段
FROM node:20-slim AS build

WORKDIR /build

# 先只 COPY 依赖清单：lock 没变时这一层命中缓存，改代码不必重装依赖。
COPY frontend/package.json frontend/package-lock.json ./

# `npm ci` 需要 lock 与 package.json 严格一致，否则直接失败 —— 这正是我们要的。
RUN npm ci

# 再 COPY 源码（tests 不参与构建，但 COPY 进去也无害）
COPY frontend/ ./

# vite build 输出到 ./dist
RUN npm run build

# ---------------------------------------------------------------- 运行阶段
FROM nginx:alpine

# 静态产物
COPY --from=build /build/dist /usr/share/nginx/html

# nginx 主配置（完整文件，含 events{} / http{} 外壳）
COPY backend/deploy/nginx.conf /etc/nginx/nginx.conf

# 健康检查用的是配置里的 location = /nginx-health
EXPOSE 80

# 官方镜像自带 ENTRYPOINT/CMD，保持默认。
#
# ⚠️ 不要加 `daemon off;` —— 官方 nginx 镜像的 CMD 已经带了。
# 在配置里再写一次会重复，某些版本会报 "daemon directive is duplicate"。