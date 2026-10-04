#!/usr/bin/env python3
"""故障隔离服务调用示例（仅依赖标准库）。

用法：
    python3 examples/demo.py [base_url]

默认 base_url 为 http://localhost:8000。

脚本完整演示：导入管网 -> 读取版本 -> 预演(不可行/可行) -> 提交 ->
版本过期拒绝 -> 重复关阀拒绝 -> 历史记录。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BASE_URL", "http://localhost:8000"
)
SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_network.json")


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入管网", status, body)
    assert status == 200, body

    # 2. 读取当前版本
    status, body = call("GET", "/api/network")
    show("2. 读取管网（拿到 version）", status, body)
    version = body["version"]

    plan = {"fault_pipe_id": "P6", "close_pipe_ids": ["P5"], "version": version}

    # 3. 预演一个不可行方案：只关 P5，D 点仍从 P4 进水
    status, body = call("POST", "/api/isolation/preview", plan)
    show("3. 预演（只关 P5，预期 isolatable=false）", status, body)

    # 4. 预演可行方案：关 P4，故障管段 P6 两端均脱离水源
    plan_ok = {**plan, "close_pipe_ids": ["P4"]}
    status, body = call("POST", "/api/isolation/preview", plan_ok)
    show("4. 预演（关 P4，预期 isolatable=true，U3 失供）", status, body)
    assert body["isolatable"] is True and body["affected_sink_ids"] == ["U3"]

    # 5. 用过期版本提交 -> 409 VERSION_CONFLICT
    status, body = call(
        "POST", "/api/isolation/submit", {**plan_ok, "version": version - 1}
    )
    show("5. 过期版本提交（预期 409）", status, body)
    assert status == 409

    # 6. 用当前版本正式提交 -> 成功，版本 +1，P4 阀位置为关闭
    status, body = call("POST", "/api/isolation/submit", plan_ok)
    show("6. 正式提交（预期成功，新版本 2）", status, body)
    assert status == 200 and body["version"] == 2

    # 7. 再次基于旧版本提交 -> 409
    status, body = call("POST", "/api/isolation/submit", plan_ok)
    show("7. 基于旧版本重复提交（预期 409）", status, body)
    assert status == 409

    # 8. 取最新版本后重复关闭 P4 -> 422 PLAN_INVALID
    status, body = call("GET", "/api/network")
    latest = body["version"]
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": latest},
    )
    show("8. 重复关闭已关阀门 P4（预期 422）", status, body)
    assert status == 422

    # 9. 隔离历史
    status, body = call("GET", "/api/isolation/events")
    show("9. 隔离操作历史", status, body)

    print("\n全部示例执行完成。")


if __name__ == "__main__":
    main()
