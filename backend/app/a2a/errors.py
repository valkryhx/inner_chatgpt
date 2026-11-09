"""A2A/JSON-RPC error types."""
from __future__ import annotations

from typing import Any


class A2AError(Exception):
    def __init__(self, code: int, message: str, *, http_status: int = 200,
                 data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.data = data

    def to_jsonrpc(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


def invalid_params(message: str = "invalid params") -> A2AError:
    return A2AError(-32602, message)


def method_not_found(method: str) -> A2AError:
    return A2AError(-32601, f"method not found: {method}")


def task_not_found(task_id: str) -> A2AError:
    return A2AError(-32001, "task not found", data={"taskId": task_id})


def task_not_cancelable(task_id: str) -> A2AError:
    return A2AError(-32001, "task is not cancelable", data={"taskId": task_id})


def internal_error(message: str = "internal error") -> A2AError:
    return A2AError(-32603, message)
