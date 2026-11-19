# workspace 单根收敛（2026-11-19）

> 涉及：`backend/app/config.py`、`backend/app/workspace.py`、
> `backend/sandbox_service/workspace.py`、`backend/deploy/docker-compose.yml`
> 测试：`backend/tests/test_single_workspace_root.py`（9 条）
> 反向确认：`docs/probes/20261119-单根收敛反向确认.py`（3/3）

## 1. 收敛前是什么

双根 + 一个白名单：

```python
def workspaces_root(user_id):
    if user_id in SANDBOX_WORKSPACE_USERS:
        return SANDBOX_WORKSPACES_DIR / user_id   # D:\sandbox\<uid>
    return WORKSPACES_DIR / user_id               # backend/data/workspaces/<uid>
```

`SANDBOX_WORKSPACE_USERS` 由 `HLZ_SANDBOX_MVP_USERS` 解析而来。

## 2. 为什么删（两个理由，第二个是决定性的）

### 理由一：双根是 Docker 方案的遗留，bwrap 下不成立

双根存在的原始需求是：沙箱 = 一个 Docker 容器，
`docker run -v D:\sandbox\<uid>:/ws` 需要挂一个「容器专属」的宿主机目录。

bwrap 用 `--bind <workspace> /ws`，**同一个路径进出**，没有「容器侧目录」
这个概念。`workspaces/` 下的目录一样能 bind。

| 双根给出的好处 | bwrap 下是否成立 |
| --- | --- |
| 容器需要独立的挂载源 | ❌ 不需要，bind 任意目录都行 |
| `D:\sandbox` 不占 C 盘 | ❌ 容器里两个根都在同一个 appdata 卷上 |
| 白名单用户隔离数据 | ❌ 隔离靠 `--bind` 单目录，与目录名无关 |

### 理由二：它已经在生产里造成错位了

2026-11-19 实测（`hlzsbxprobe` 栈）：

```bash
$ docker exec app      python -c "from app.workspace import workspaces_root; ..."
app     thinks u_c56b8337dd09 -> /app/backend/data/workspaces/u_c56b8337dd09

$ docker exec sandbox  python -c "from sandbox_service.workspace import workspaces_root; ..."
sandbox thinks u_c56b8337dd09 -> /app/backend/data/sandbox/u_c56b8337dd09
```

**成因**：`HLZ_SANDBOX_MVP_USERS` 在两个容器的 compose 里默认值写法不同：

```yaml
app:     HLZ_SANDBOX_MVP_USERS: ${HLZ_SANDBOX_MVP_USERS-u_c56b8337dd09,u_343a46896bff}
sandbox: HLZ_SANDBOX_MVP_USERS: ${HLZ_SANDBOX_MVP_USERS-u_c56b8337dd09,u_343a46896bff}
```

看起来一样，但当宿主的 `.env` 里写了 `HLZ_SANDBOX_MVP_USERS=`（空串）时，
两边的**解析**不同（`${VAR-def}` 与 `${VAR:-def}` 对空串的处理不同），
于是容器里看到不同的值。这正是设计文档 `20261118` §6.3 警告的
「file_write 写进 A、file_read 读 B」——它当时就在生产里活着。

**它是潜伏的**：只有当白名单用户真的跑代码才暴露，而那时不会报错，
只会「刚写的文件不见了」。

## 3. 收敛后

```python
def workspaces_root(user_id: str) -> Path:
    _reject_unsafe_segment(user_id)
    return WORKSPACES_DIR / user_id
```

**不变量：一个用户，一个根，不受任何沙箱开关影响。**

沙箱开关（`MODE` / `BACKEND` / `MVP_USERS`）只决定「走不走沙箱」，
**不决定文件放哪**。这两件事分开之后，就不可能再错位。

### 删除清单

| 位置 | 删除内容 |
| --- | --- |
| `app/config.py` | `default_sandbox_root()`、`SANDBOX_WORKSPACES_DIR`、`SANDBOX_WORKSPACE_USERS` |
| `app/workspace.py` | 白名单分支 |
| `sandbox_service/workspace.py` | `SANDBOX_WORKSPACES_ENV`、`DEFAULT_SANDBOX_WORKSPACES_DIR`、`sandbox_workspaces_dir()`、`mvp_users()`、`DEFAULT_MVP_USERS` |
| `docker-compose.yml` | 两个服务上的 `HLZ_SANDBOX_WORKSPACES_DIR` |

`HLZ_SANDBOX_MVP_USERS` **保留**：它仍然决定「谁有权进沙箱」
（`sandbox/config.py::user_in_scope`），只是不再影响路径。
compose 里两个服务上的写法现在**逐字相同**（`${VAR-}`），有测试钉住。

### 删掉/重写的测试

| 文件 | 处置 |
| --- | --- |
| `test_config_sandbox_workspaces.py` | **删除**（整文件在测已删的常量） |
| `test_sandbox_workspace_root_platform.py` | **删除**（测 `default_sandbox_root` 的平台分支，函数已删） |
| `test_sandbox_service_workspace.py` | **重写**（双根用例删除，保留路径安全 + 两侧一致性） |
| `test_sandbox_seams.py` | 删掉白名单 patch；`test_auto_degrade_file_write_still_writes` 的期望路径从 `sandbox/u_mvp` 改成 `ws/u_mvp` |
| `test_sandbox_multiagent.py` | 删掉对已删常量的 patch |

## 4. 验收

| 项 | 结果 |
| --- | --- |
| 全量测试 | **1607 passed, 66 skipped** |
| 新增测试 | `test_single_workspace_root.py` 9 条 |
| 反向确认 | **3/3**（白名单分支加回 / 沙箱侧分叉 / compose 默认值不同） |
| 生产两侧路径 | 逐字一致（修复前 `workspaces/` vs `sandbox/`） |
| 沙箱写 → app 进程内读 | 读到同一份（`FROM_SANDBOX`） |
| 端到端 | 6/6 |

## 5. 遗留

- 旧数据：`backend/data/sandbox/` 目录（如果存在）现在是孤儿，
  里面有历史上白名单用户的数据。**没有自动迁移** —— 因为实测该目录是空的
  （白名单用户从未真正在那里跑过，这正是错位的另一面）。
  若某部署里它非空，需要人工判断合并。
- `docs/20261118-沙箱MVP设计与实施.md` §6.3 描述的仍是双根方案。
  按仓库规矩历史文档不改（那是当时的真实状态），本文件是它的修订说明。