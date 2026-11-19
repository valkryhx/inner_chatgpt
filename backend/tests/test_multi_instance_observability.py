"""多实例可观测性：日志必须能回答「这是哪个实例、在哪个节点上」。

## 要解决的问题（真跑 compose --scale app=3 时暴露）

1. **所有副本共享同一个 `appdata` 卷，写同一个 `app.log`** ——
   三个实例的日志交错在一起，`tail app.log` 看不出某一行是哪个实例打的。
   排查「为什么这个请求落到了 B」「B 为什么判 A 死了」时无从下手。

2. **/api/health 只返回 `{ok, model, mcp_tools}`** ——
   运维拿不到「我是谁、我连的哪个 Redis、当前集群有几个实例」，
   只能进容器 exec。

3. **owner 只在响应头里**（`X-HLZ-Instance`），日志行里没有，
   所以「日志」和「HTTP 请求」两个视图对不上。

## 修法

- 日志行前缀**恒定包含实例标识**（`[<owner>]`），JSONL 里作为 `instance` 字段；
- `app.log` 按实例分文件（`app-<owner>.log`），共享卷也不会交错；
- `/api/health` 扩展返回实例身份 + 集群成员数（不含敏感信息）；
- 启动/登记/心跳关键节点都打结构化日志，字段可 grep。

## 反向确认

- 去掉 `InstanceFilter`/前缀注入 -> `test_log_line_carries_instance_prefix` 变红
- 去掉 `HealthOut.instance` 字段 -> `test_health_reports_instance_identity` 变红
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


from _logging_isolation import fresh_logging_scope, isolated_logging  # noqa: E402,F401


@pytest.fixture()
def fresh_logging(isolated_logging):
    """日志配置隔离（统一实现见 `tests/_logging_isolation.py`）。

    先前这里手写的版本漏了 team / prelude 两个独立 logger 的 handler ——
    表现为「单跑绿、全量跑红」，因为前一个测试文件装满的 handler 还在。
    """
    return isolated_logging


# ---------------------------------------------------------------- 日志带实例标识

def test_log_line_carries_instance_prefix(fresh_logging, monkeypatch, capfd):
    """**核心**：每一行人读日志都要能看出是哪个实例打的。

    三个副本共享同一个卷、写同一份日志时，没有这个前缀就无法归属。
    """
    monkeypatch.setenv("HLZ_LOG_LEVEL", "INFO")
    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-a")
    from app import logging_setup
    logging_setup.setup_logging()

    logging.getLogger("app.demo").info("跨实例排查信息")
    out = capfd.readouterr()
    text = out.out + out.err
    assert "pod-a" in text, f"日志行没有实例标识：{text!r}"


def test_console_prefix_matches_owner_tail(fresh_logging, monkeypatch, capfd):
    """前缀里的标识必须与 `owner_id()` 的尾部一致 —— 否则日志与
    `X-HLZ-Instance` 响应头对不上，排查时无法互相印证。"""
    monkeypatch.setenv("HLZ_LOG_LEVEL", "INFO")
    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-x")
    from app import logging_setup
    from app.sandbox.identity import _instance_tag, owner_id
    logging_setup.setup_logging()

    logging.getLogger("app.demo").info("对齐检查")
    text = capfd.readouterr()
    tag = _instance_tag()
    owner = owner_id()
    assert tag in text.out + text.err
    assert owner.split(":", 1)[1] == tag, f"owner 尾部与日志标识不一致：{owner}"


def test_app_log_file_is_per_instance(fresh_logging, monkeypatch):
    """`app.log` 按实例分文件，否则共享卷下三个副本交错写同一个文件。"""
    monkeypatch.setenv("HLZ_LOG_LEVEL", "INFO")
    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-a")
    from app import logging_setup
    cfg = logging_setup.setup_logging()
    assert "pod-a" in cfg["app_log"], f"app.log 未按实例区分：{cfg['app_log']}"

    logging.getLogger("app.demo").info("写进本实例的文件")
    p = Path(cfg["app_log"])
    assert p.exists(), f"{p} 没生成"
    assert "写进本实例的文件" in p.read_text(encoding="utf-8")


def test_per_instance_log_files_do_not_interleave(fresh_logging, monkeypatch):
    """两个不同标识写出的文件必须互不相干。"""
    from app import logging_setup

    monkeypatch.setenv("HLZ_LOG_LEVEL", "INFO")
    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-a")
    cfg_a = logging_setup.setup_logging()
    logging.getLogger("app.a").info("A 的日志")
    for h in logging.getLogger().handlers[:]:
        if hasattr(h, "close"):
            h.close()
    logging.getLogger().handlers.clear()
    logging_setup._configured = False

    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-b")
    cfg_b = logging_setup.setup_logging()
    logging.getLogger("app.b").info("B 的日志")

    a_text = Path(cfg_a["app_log"]).read_text(encoding="utf-8")
    b_text = Path(cfg_b["app_log"]).read_text(encoding="utf-8")
    assert cfg_a["app_log"] != cfg_b["app_log"]
    assert "A 的日志" in a_text and "B 的日志" not in a_text
    assert "B 的日志" in b_text and "A 的日志" not in b_text


def test_team_jsonl_carries_instance_field(fresh_logging, monkeypatch):
    """团队事件 JSONL 里要有 `instance` 字段，便于按实例过滤。"""
    monkeypatch.setenv("HLZ_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-a")
    from app import logging_setup
    logging_setup.setup_logging()

    logging_setup.team_event("message_delivered", agent="alpha")
    path = fresh_logging / f"team-{time.strftime('%Y%m%d')}.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert row.get("instance"), f"团队事件没有 instance 字段：{row}"
    assert "pod-a" in row["instance"]


def test_instance_field_does_not_leak_into_message(fresh_logging, monkeypatch, capfd):
    """注入字段不能污染 `record.getMessage()` —— 否则任何断言消息内容的
    代码都会失败（日志前缀要加在 Formatter 层，不是改 msg）。"""
    monkeypatch.setenv("HLZ_LOG_LEVEL", "INFO")
    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-a")
    from app import logging_setup
    logging_setup.setup_logging()

    rec = logging.getLogger("app.demo").makeRecord(
        "app.demo", logging.INFO, __file__, 1, "纯消息体", (), None)
    logging.getLogger("app.demo").handle(rec)
    assert rec.getMessage() == "纯消息体", f"消息被污染：{rec.getMessage()!r}"


# ---------------------------------------------------------------- /api/health 扩展

def test_health_reports_instance_identity(monkeypatch):
    """health 必须能回答「我是谁」「集群里还有谁」—— 运维不进容器就能看到。"""
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app

    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-a")
    with TestClient(fastapi_app) as c:
        r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    # 原有字段必须保持（前端依赖）
    assert body["ok"] is True
    assert "model" in body and "mcp_tools" in body
    # 新增：实例身份
    assert body.get("instance"), f"health 没有实例标识：{body}"
    assert "pod-a" in body["instance"]


def test_health_reports_cluster_shape(monkeypatch):
    """要能看出「当前是单实例还是多实例、Redis 连不连得上」。"""
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as c:
        r = c.get("/api/health")
    body = r.json()
    assert "cluster" in body, f"health 没有集群信息：{body}"
    cluster = body["cluster"]
    assert "enabled" in cluster, f"cluster 缺 enabled：{cluster}"
    assert isinstance(cluster["enabled"], bool)
    # 没配 Redis 时必须是 False，且 live_instances 为 0（不是 null）
    assert cluster["enabled"] is False
    assert cluster["live_instances"] == 0


def test_health_does_not_leak_secrets(monkeypatch, tmp_path):
    """集群信息里绝不能带 Redis URL / 密码。

    ⚠️ 这里用**连不上**的 Redis 地址（`:1`）：目的是验证序列化结果不含密码，
    不是验证连通性。用真实容器会让测试依赖外部环境。
    连不上时 health 必须降级（`live_instances=0`）而不是 500。
    """
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app

    # DB 用临时 sqlite（不设 HLZ_DB_URL 会落到默认库，污染真实数据）
    monkeypatch.delenv("HLZ_DB_URL", raising=False)
    monkeypatch.setenv("HLZ_REDIS_URL", "redis://:supersecret@127.0.0.1:1/0")
    with TestClient(fastapi_app) as c:
        r = c.get("/api/health")
    assert r.status_code == 200, f"health 不该因 Redis 连不上而挂：{r.status_code}"
    text = r.text
    assert "supersecret" not in text, f"health 泄露了 Redis 密码：{text}"
    assert "redis://" not in text, f"health 回显了连接串：{text}"
    # 降级：连不上时 live_instances 必须是 0，不是 null 也不是 500
    assert r.json()["cluster"]["live_instances"] == 0


# ---------------------------------------------------------------- 启动期结构化日志

def test_cluster_startup_logs_are_structured(fresh_logging, monkeypatch, caplog):
    """登记 / 心跳 / 存活集合这几个关键节点要有可 grep 的字段。"""
    from app.cluster import instances

    monkeypatch.setenv("HLZ_INSTANCE_ID", "pod-a")
    monkeypatch.setenv("HLZ_REDIS_URL", "redis://127.0.0.1:0/0")

    class FakeRedis:
        async def zadd(self, key, mapping):
            return 1

        async def pexpire(self, key, ms):
            return True

        async def get(self, key):
            return None

        async def set(self, key, value, *, px=None, nx=False):
            return True

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr(instances, "get_redis", fake_get_redis)

    import asyncio
    with caplog.at_level(logging.INFO, logger=instances.logger.name):
        assert asyncio.run(instances.register_self()) is True

    msgs = [r.getMessage() for r in caplog.records]
    joined = " ".join(msgs)
    assert "登记" in joined or "instance" in joined.lower(), \
        f"登记没有日志：{msgs}"