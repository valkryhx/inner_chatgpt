"""Hlaudz 沙箱服务（bwrap 运行时）。

独立服务，与 app 解耦。方案见 `docs/20261119-沙箱服务化方案-bwrap版.md`。

启动：
    uvicorn sandbox_service.main:app --host 0.0.0.0 --port 8021
"""