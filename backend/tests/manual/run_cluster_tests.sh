#!/usr/bin/env bash
# 一键跑「需要真实依赖」的测试（2026-11-19 审计 §5）。
#
# ## 为什么需要它
#
# 52 条测试默认被跳过，因为它们需要真 Redis / 真 PG：
#
#     24 个  未设置 HLZ_TEST_REDIS_URL，跳过 Redis 集成测试
#     20 个  未设置 HLZ_TEST_PG_DSN，跳过 PG 集成测试
#      4 个  未设置 HLZ_TEST_PG_DSN，跳过 PG 测试
#
# 这些测试**质量很好**（实测 60 + 45 全过），问题是没人知道怎么跑 ——
# 环境变量只写在 docs 里，没有入口。于是「多实例改对了吗」只能靠人肉跑探针。
#
# ## 用法
#
#     bash backend/tests/manual/run_cluster_tests.sh              # 用默认端口
#     bash backend/tests/manual/run_cluster_tests.sh --check      # 只检查依赖是否就绪
#
#     HLZ_REDIS_URL=redis://127.0.0.1:16379/0 \
#     HLZ_PG_DSN=postgresql://hlz:pw@127.0.0.1:15432/hlz \
#       bash backend/tests/manual/run_cluster_tests.sh
#
# ## 容器从哪来
#
# 本仓库的多实例部署用 compose 起 `hlz-redis`(16379) 与 `hlz-pg-eval`(15432)。
# 脚本不负责起容器 —— 起容器是有副作用的动作，不该藏在测试入口里。
# 依赖不在时会明确告诉你缺什么，而不是静默跳过。

set -uo pipefail

REDIS_URL="${HLZ_REDIS_URL:-redis://127.0.0.1:16379/0}"
PG_DSN="${HLZ_PG_DSN:-postgresql://hlz:eval123@127.0.0.1:15432/hlz}"
CHECK_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$(cd "$HERE/../.." && pwd)"
cd "$BACKEND" || exit 1

# ---------------------------------------------------------------- 依赖检查

need=0

check_redis() {
  local hostport
  # 从 URL 里抠 host:port：redis://host:port/db -> host:port
  hostport=$(printf '%s' "$REDIS_URL" | sed -E 's#^redis://##; s#/.*$##')
  if (echo > "/dev/tcp/${hostport%:*}/${hostport#*:}") 2>/dev/null; then
    echo "  [ok]   Redis  $hostport"
  else
    echo "  [缺]   Redis  $hostport  —— 起容器：docker compose -f backend/deploy/docker-compose.yml up -d redis"
    need=1
  fi
}

check_pg() {
  local hostport
  # postgresql://user:pw@host:port/db -> host:port
  hostport=$(printf '%s' "$PG_DSN" | sed -E 's#^postgresql://##; s#@#\n#; s#^[^\n]*\n##; s#/.*$##')
  if (echo > "/dev/tcp/${hostport%:*}/${hostport#*:}") 2>/dev/null; then
    echo "  [ok]   PG     $hostport"
  else
    echo "  [缺]   PG     $hostport  —— 起容器：docker compose -f backend/deploy/docker-compose.yml up -d pg"
    need=1
  fi
}

echo "检查真实依赖："
check_redis
check_pg
echo

if [ "$CHECK_ONLY" = "1" ]; then
  if [ "$need" = "0" ]; then echo "依赖齐全，可以跑全量。"; else echo "依赖不全（见上）。"; fi
  exit "$need"
fi

if [ "$need" != "0" ]; then
  echo "依赖不齐，先用 --check 看缺什么，或起好容器再来。"
  echo "（不想起容器也可以跑默认的 SQLite 套件：cd backend && python -m pytest tests -q）"
  exit 1
fi

# ---------------------------------------------------------------- 跑测试

export HLZ_TEST_REDIS_URL="$REDIS_URL"
export HLZ_TEST_PG_DSN="$PG_DSN"
export HLZ_PG_SCHEMA_PER_PATH=1     # PG 测试隔离：每个路径一个独立 schema

# 需要真实依赖的测试文件（grep 出来的，不是猜的）。
# 新增需要依赖的测试时要加到这里 —— 否则它永远被静默跳过。
# 校验：python tools/verify/verify_cluster_test_list.py（漏加会退出码 1）
CLUSTER_TESTS=(
  tests/test_cluster.py
  tests/test_cluster_global_scope.py
  tests/test_cluster_invalidate.py
  tests/test_cluster_steer.py
  tests/test_cluster_stop.py
  tests/test_ddl_concurrency.py
  tests/test_lock_scope.py
)

echo "跑 Redis 相关（多实例协调层）："
python -m pytest "${CLUSTER_TESTS[@]}" -q
rc_cluster=$?

echo
echo "跑 PG 相关（方言兼容 / 后端切换）："
python -m pytest tests/test_pg_backend.py tests/test_sqlcompat.py -q
rc_pg=$?

echo
echo "=============================================="
if [ "$rc_cluster" = "0" ] && [ "$rc_pg" = "0" ]; then
  echo "全部通过"
  exit 0
fi
echo "有失败：cluster=$rc_cluster pg=$rc_pg"
exit 1