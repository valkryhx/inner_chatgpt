"""校验《沙箱服务化方案（bwrap 版）》的坐标与断言。

## 为什么需要

这份文档通篇是「A 文件 B 行这么做」——行号错一个，读者按图索骥就看到
完全不同的事实。而这类错**读起来完全不别扭**（AGENTS.md §8.6「自洽 ≠ 正确」）。

## 校验什么

1. 现有代码的坐标（`VALID_MODES` / `exec_run` / `_client` / `write_root_for`）
2. Aivory 侧的坐标（compose 的 SECURITY NOTE、`/exec` 的实现、稳定 archive key）
3. 否定结论（must_not）：
   - app 镜像里没有 minio/boto3
   - Aivory 的主 compose 里没有 code interpreter 服务（在评估文档里已验，这里不重复）
4. 文档自身：关键数字、五个坑、四形态表、"未经运行时验证" 声明

用法：
    python docs/probes/20261119-沙箱服务化方案核验.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(os.environ.get("HLZ_PLAN_REPO") or Path(__file__).resolve().parents[2])
BACKEND = REPO / "backend"
AIVORY = Path(os.environ.get("HLZ_AIVORY") or r"D:\git_codes\Aivory")
DOC = Path(os.environ.get("HLZ_PLAN_DOC")
           or (REPO / "docs" / "20261119-沙箱服务化方案-bwrap版.md"))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [OK]   {name}")
    else:
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILS.append(name)


def must_not(name: str, cond_absent: bool, detail: str = "") -> None:
    """否定结论：条件为真 = 通过。调用方传 `not in` 形式的布尔值。"""
    check(f"[must_not] {name}", cond_absent, detail)


def line_of(path: Path, n: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"<读取失败 {exc}>"
    if 1 <= n <= len(lines):
        return lines[n - 1]
    return f"<第 {n} 行不存在，共 {len(lines)} 行>"


def code_lines(path: Path) -> str:
    """剥掉注释行和行尾注释，只留可执行代码。

    ⚠️ 必须有这一步：文档的坑会**合法地**在注释里出现旧写法，
    纯文本匹配会把注释算成违规（AGENTS.md §8.5 说的同一类问题，
    本项目反向确认实际抓到过）。
    """
    out = []
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


def main() -> int:
    print("=" * 70)
    print("一、hlaudz 现有代码坐标")
    print("=" * 70)

    cfg = BACKEND / "app" / "sandbox" / "config.py"
    l = line_of(cfg, 30)
    check("config.py:30 是 VALID_MODES",
          "VALID_MODES" in l and "off" in l and "auto" in l and "strict" in l, l[:90])

    l = line_of(cfg, 90)
    check("config.py:90 读 HLZ_SANDBOX_MODE",
          "HLZ_SANDBOX_MODE" in l, l[:90])

    rt = BACKEND / "app" / "sandbox" / "runtime.py"
    src_rt = rt.read_text(encoding="utf-8", errors="replace")
    l = line_of(rt, 65)
    check("runtime.py:65 是 _client", l.startswith("def _client"), l[:80])
    check("runtime.py 真的调 docker.from_env()", "docker.from_env()" in src_rt)
    # exec 主路径是 exec_run + argv 列表（每次新进程）
    check("runtime.py 用 exec_run 执行", "exec_run(" in src_rt)
    check("exec 走 argv 列表（不是常驻 REPL）",
          'argv = ["sh", "-c", code]' in src_rt or "argv=[" in src_rt)

    mgr = BACKEND / "app" / "sandbox" / "manager.py"
    src_mgr = mgr.read_text(encoding="utf-8", errors="replace")
    check("manager.py 有 DEGRADED_PREFIX", "DEGRADED_PREFIX" in src_mgr)
    check("manager.py 处理 strict 模式", 'mode == "strict"' in src_mgr)
    l = line_of(mgr, 72)
    check("manager.py:72 是 _host_workspace",
          l.startswith("def _host_workspace"), l[:80])

    # 反向依赖（文档说这是拆分难点）
    check("manager.py 反向 import write_root_for",
          "from ..workspace import write_root_for" in src_mgr)

    print()
    print("=" * 70)
    print("二、否定结论（must_not）：app 没有对象存储")
    print("=" * 70)

    hits = 0
    for p in BACKEND.rglob("*.py"):
        t = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\b(minio|boto3|s3_client)\b", t, re.I):
            hits += 1
    must_not("backend 里没有 minio / boto3 依赖", hits == 0, f"命中 {hits} 个文件")

    print()
    print("=" * 70)
    print("三、Aivory 侧坐标")
    print("=" * 70)

    if not AIVORY.exists():
        print(f"  [SKIP] {AIVORY} 不存在")
    else:
        svc = AIVORY / "sandbox-service"
        compose = svc / "docker-compose.yml"
        app_py = svc / "app.py"

        check("Aivory sandbox-service 存在", svc.exists() and app_py.exists())

        src_compose = compose.read_text(encoding="utf-8", errors="replace")
        # 文档引用：它自认架构上需要 sock
        check("compose 里有 docker.sock 挂载",
              "/var/run/docker.sock" in src_compose)
        check("compose 里 SECURITY NOTE 承认 root 等价",
              "root-equivalent" in src_compose, "未找到 root-equivalent")
        # ⚠️ 断言必须能跨行匹配：原句在 compose 注释里是断行的
        # （"...The architecture REQUIRES the\n# socket, so it is intentionally kept"），
        # 单行子串匹配会假红。把注释前缀剥掉再压成一行。
        flat = re.sub(r"\n\s*#\s*", " ", src_compose)
        check("compose 里写了「架构需要 socket」",
              "The architecture REQUIRES the socket" in flat,
              "未找到（已按跨行归一化）")
        check("compose 里 local 归档卷",
              "SANDBOX_LOCAL_STORAGE_DIR" in src_compose)
        check("compose 注释说 multi-replica 需 S3/OSS",
              "multi-replica" in src_compose and "S3" in src_compose)

        src_app = app_py.read_text(encoding="utf-8", errors="replace")
        # 文档说：Aivory 的 /exec 每次新起 python 进程
        check("Aivory /exec 每次跑 python cell_path",
              "python", "cell_path" in src_app and '["timeout"' in src_app)
        # 稳定 archive key
        check("Aivory 有稳定 archive key 机制",
              "_session_archive_key" in src_app)
        check("Aivory 注释写明稳定 key 的原因",
              "stable archive key" in src_app or "stable key" in src_app)
        check("Aivory 有 boto3/oss2 懒加载",
              "boto3" in src_app and "oss2" in src_app)

        # 行号断言（文档 §11 引用了 224 行）
        l = line_of(app_py, 224)
        check("Aivory app.py:224 是稳定 key 的注释",
              "archive" in l.lower() or "stable" in l.lower(), l[:90])

        # 端点存在性
        for ep in ('@app.post("/sessions")', '@app.post("/exec")',
                   '@app.post("/files")'):
            check(f"Aivory 端点 {ep}", ep in src_app)

    print()
    print("=" * 70)
    print("四、文档自身")
    print("=" * 70)

    doc = DOC.read_text(encoding="utf-8")
    check("文档存在且非空", len(doc) > 15000, f"{len(doc)} 字符")
    check("文档标注了「未经运行时验证」", "未经运行时验证" in doc)
    check("文档区分实测与设计", "实测" in doc and "设计" in doc)

    # 核心结论必须都在
    for key in ("不要 MinIO", "纯净父进程", "bwrap", "共享卷"):
        check(f"文档含关键结论：{key}", key in doc)

    # 三种池形态 -> 必须是四种（文档已从 3 改 4）
    check("文档给出四种池形态", "四种池形态" in doc)
    must_not("文档里没有旧的「三种池形态」", "三种池形态" not in doc)

    # 五个 bwrap 坑
    for pit in ("--clearenv", "--cap-drop ALL", "--tmpfs /etc", "最小挂载集"):
        check(f"bwrap 规范含 {pit}", pit in doc)

    # 关键数字（实测值，错了说明没复核）
    for num in ("6 ms", "618 ms", "1105 ms", "8.8 ms", "5528 ms"):
        check(f"文档含实测数字 {num}", num in doc)

    # 开关兼容是硬要求
    check("文档写了 off 分支必须保持现状",
          "off`（默认）" in doc or "逐字节保持现状" in doc)
    check("文档写了回滚能力是硬要求", "回滚能力是硬要求" in doc)

    # 订正声明（§2 是订正，必须留着）
    check("文档保留了 §2 的订正", "订正" in doc)

    print()
    print("=" * 70)
    print("五、§2.5 与 GPT 讨论文档的对照")
    print("=" * 70)

    check("有 §2.5 对照章节", "## 2.5 与 `20261118-与gpt讨论沙箱.md`" in doc)
    # 必须承认对方说对了内核边界这条
    check("承认 bwrap 没有内核攻击面边界",
          "共享宿主内核" in doc and "没有独立内核攻击面" in doc)
    # 必须写明用户是内部可信（这是选 bwrap 的前提）
    check("写明用户是内部可信",
          "内部用户" in doc or "内部可信" in doc)
    # 必须写明 gVisor 本机不可用的实测证据
    check("写明 /dev/kvm 不存在",
          "/dev/kvm" in doc)
    # 必须给出 rootfs 那条的订正（不等于全盘接受 A.1）
    check("对 A.1 的 rootfs 结论做了订正",
          "--ro-bind /opt/rootfs" in doc or "/opt/sbx-rootfs" in doc)

    print()
    print("=" * 70)
    print("六、§8.2 Executor 接口（选 A 的技术前提）")
    print("=" * 70)

    check("有 Executor 接口章节", "## 8.2 Executor 接口" in doc)
    for impl in ("BwrapExecutor", "GVisorExecutor", "InprocExecutor"):
        check(f"列出实现 {impl}", impl in doc)
    check("写明 off 模式走 InprocExecutor",
          "`off` 模式下走 `InprocExecutor`" in doc)
    check("写明换运行时不该改什么",
          "不应该改" in doc)
    check("有 §9.5 上 gVisor 的检查清单", "## 9.5" in doc)

    print()
    print("=" * 70)
    print("七、已定决策必须留痕")
    print("=" * 70)

    check("写明路线选 A", "先 bwrap，后补 gVisor" in doc)
    check("决策标注了日期", "2026-11-19" in doc)

    print()
    print("=" * 70)
    print("八、§9 实施阶段（每段都要有 产出/任务/验收/不做什么）")
    print("=" * 70)

    check("有 §9.0 总览", "### 9.0 总览" in doc)
    # 五个阶段必须都在
    for stage in ("阶段 0（已完成）", "阶段 1：沙箱服务骨架",
                  "阶段 2：app 侧接入", "阶段 3：池 + 归档",
                  "阶段 4（按需）"):
        check(f"有 {stage}", stage in doc)

    # 阶段 1~3 必须有完整四件套
    for n in ("1", "2", "3"):
        for part in ("产出文件", "任务清单", "验收", "不做什么"):
            check(f"阶段 {n} 有「{part}」", f"#### {n}.{'1234'[('产出文件','任务清单','验收','不做什么').index(part)]} {part}" in doc)

    # 每条隔离断言都必须有反向确认（AGENTS.md §8.5）
    check("阶段 1 写了反向确认表", "反向确认（AGENTS.md §8.5 强制）" in doc)
    for t in ("test_cannot_reach_network", "test_cannot_write_usr",
              "test_cross_tenant_isolation", "test_cannot_read_host_etc_shadow"):
        check(f"列出隔离断言 {t}", t in doc)

    # 阶段 2 的关键：off 模式必须验「0 次网络调用」
    check("off 模式验收要求网络层 0 次调用",
          "网络层 0 次调用" in doc)
    check("有「service 后端真的被用了」的反向测试",
          "test_service_backend_is_actually_used" in doc)

    # 阶段 3 的关键：fork 三条铁律 + 稳定 key
    check("阶段 3 列出 fork 三条铁律", "三条铁律" in doc)
    check("阶段 3 要求 os._exit", "os._exit()" in doc)
    check("阶段 3 有跨租户内存泄漏测试",
          "test_no_cross_tenant_memory_leak" in doc)
    check("阶段 3 归档 key 用稳定标识",
          "test_archive_key_is_stable" in doc)

    # 阶段 1 不许写池（先跑通）
    check("阶段 1 明确不做池", "不做池（阶段 3）" in doc)
    # 任何阶段都不许引入 docker.sock 给 app
    check("明确不引入 docker.sock",
          "不引入 docker.sock" in doc)

    print()
    print("=" * 70)
    print("九、§4 池的形态（必须明确回答「一个还是多个」）")
    print("=" * 70)

    # 直接回答「是池」
    check("§4.0 明确回答是池", "是池，而且必须是池" in doc)
    # 关键否定结论：不能一个共享父进程轮换 workspace
    check("记录了「一个槽不能轮换服务多个用户」的实测",
          "unshare(CLONE_NEWNS)=EPERM" in doc)
    check("明确槽 = 用户", "槽 = 用户" in doc)
    # 内存预算必须写清楚，且必须说明 RSS 会高估
    check("§4.5 有内存预算", "### 4.5 内存预算" in doc)
    check("说明 RSS 相加会重复计算共享页",
          "重复计算共享库页" in doc)
    check("给出单槽真实占用", "49 MB" in doc)
    # 池在服务里，不在 app 里
    check("§4.6 说明池在沙箱服务里",
          "### 4.6 池与「多实例」的关系" in doc)
    # 四形态必须说明是「槽内」的区别
    check("四种形态限定为槽内执行方式",
          "讲的都是**单个槽内部**怎么执行" in doc)

    print()
    print("=" * 70)
    print("十、§4.7 动态扩缩容")
    print("=" * 70)

    check("有 §4.7 扩缩容章节", "### 4.7 动态扩缩容" in doc)
    check("明确「不能预建通用槽」", "不能：预先建「通用槽」" in doc)
    # 旧断言「能按需建、按 LRU 淘汰」已随设计变更过期：
    # LRU 淘汰现在只是 OVERFLOW=evict 的一个分支，不再是主路径。
    check("明确「能按需建、按需回收」", "能：按需建、按需回收" in doc)
    # 关键实测数字
    check("写出建槽成本 27 ms", "27 ms" in doc)
    # 语义必须写清楚：MAX_SLOTS 是保护阀不是池大小
    check("明确 MAX_SLOTS 是保护阀", "保护阀" in doc)
    # 排队要可观测
    check("排队等待要可观测（queue_wait_ms）", "queue_wait_ms" in doc)
    # 不许淘汰忙碌槽
    check("明确不淘汰忙碌槽", "- ❌ **不淘汰忙碌中的槽**" in doc)

    print()
    print("=" * 70)
    print("十一、§4.7.3 / §6.2.1 OVERFLOW 开关（三个取值含义必须写清）")
    print("=" * 70)

    check("有 OVERFLOW 章节", "#### 4.7.3 槽满了怎么办" in doc)
    # 三个取值都要出现，且各自的含义要能读到
    for val in ("`queue`", "`evict`", "`reject`"):
        check(f"OVERFLOW 取值 {val} 已定义", val in doc)
    check("OVERFLOW 默认是 queue", "**`queue`**（默认）" in doc)
    # 每个取值的代价必须写明
    check("queue 写了代价（第 N+1 个用户变慢）", "第 N+1 个用户变慢" in doc)
    check("evict 写了代价（被淘汰者重新预热）", "重新预热" in doc)
    check("reject 写了代价（直接失败）", "直接失败" in doc)
    # 共同点：忙碌槽不淘汰 + 淘汰前归档
    check("三取值共同点：忙碌槽永不淘汰", "忙碌中的槽永不淘汰" in doc)
    check("三取值共同点：淘汰前归档", "淘汰前必须归档" in doc)
    # evict 全忙时退化成 queue（硬规则）
    check("evict 全忙时退化成 queue", "退化成 `queue`" in doc)
    # 非法值兜底
    check("非法 OVERFLOW 取值退回 queue", "非法取值一律退回 `queue`" in doc)
    # §6.2.1 的完整取值表
    check("§6.2.1 有 MODE 取值表", "**① `HLZ_SANDBOX_MODE`**" in doc)
    check("§6.2.1 有 BACKEND 取值表", "**② `HLZ_SANDBOX_BACKEND`**" in doc)
    check("§6.2.1 有 OVERFLOW 取值表", "**③ `HLZ_SANDBOX_OVERFLOW`**" in doc)
    check("§6.2.2 有组合矩阵", "### 6.2.2 三个开关的组合矩阵" in doc)
    # mode=off 时后端配置被忽略（兼容性核心）
    check("mode=off 时后端被忽略", "后端配置被完全忽略" in doc)

    print()
    print("=" * 70)
    print("十二、§4.7.5 内存预算（单机 3 并发口径）")
    print("=" * 70)

    check("有内存预算小节", "#### 4.7.5 内存预算" in doc)
    check("给出 1 槽 / 3 槽实测值", "27 MB" in doc and "143 MB" in doc)
    check("给出单槽边际成本 53 MB", "53 MB" in doc)
    check("给出 3 槽 + 执行余量峰值", "443 MB" in doc)
    check("给出当前部署推荐配置", "HLZ_SANDBOX_MAX_SLOTS=3" in doc)
    check("说明旧数字口径不同要重测", "必须在本机重测" in doc)
    # 关于「初始化 1 个槽」必须明确不建议
    check("§4.7.6 明确不建议预建", "#### 4.7.6 关于「初始化 1 个槽」" in doc)
    # 阶段 3 必须有扩缩容任务与测试
    check("阶段 3 有动态扩缩容任务", "3.6 | **动态扩缩容**" in doc)
    for t in ("test_slot_created_on_demand",
              "test_overflow_queue_blocks_until_slot_frees",
              "test_overflow_evict_removes_lru_idle_slot",
              "test_overflow_evict_falls_back_to_queue_when_all_busy",
              "test_overflow_reject_returns_503_immediately",
              "test_never_evicts_busy_slot",
              "test_queue_wait_is_reported"):
        check(f"列出扩缩容测试 {t}", t in doc)
    # gVisor 下不能套用这套模型
    check("§9.5 说明 gVisor 不能套用实时扩缩容",
          "不能直接套到 gVisor 上" in doc)

    print()
    print("=" * 70)
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())