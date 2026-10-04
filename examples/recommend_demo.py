#!/usr/bin/env python3
"""关阀建议调用示例（仅依赖标准库）。

用法：
    python3 examples/recommend_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/sample_network.json）：

    S --P1[阀]-- A --P2[阀]-- B --P4[阀]-- D --P6(无阀,故障)-- U3
                   |           |
                 P3[阀]       P5[阀]
                   |           |
                  U1          U2

脚本依次演示：
  1. 导入管网（版本 1）
  2. 只知道故障管段 P6：请求关阀建议 -> 建议关 P4，预计 U3 失供
  3. 按建议直接提交隔离 -> 成功（版本 2）
  4. 对已隔离的 P6 再次建议 -> 空方案（两端已与水源断开）
  5. 新故障 P5：建议不含已关闭的 P4 -> 建议关 P2，预计 U2 失供
  6. 按过期版本提交建议 -> 409 VERSION_CONFLICT（沿用现有冲突处理）
  7. 按建议返回的当前版本提交 -> 成功（版本 3）
  8. 故障管段直连水源（P1）-> feasible=false，NO_FEASIBLE_PLAN
  9. 故障管段不存在（PX）-> feasible=false，FAULT_PIPE_NOT_FOUND
 10. 读取管网确认建议全程只读（版本仍为 3）
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
    assert status == 200 and body["version"] == 1, body

    # 2. 只知道故障管段 P6：请求关阀建议
    status, body = call(
        "POST", "/api/isolation/recommend", {"fault_pipe_id": "P6", "version": 1}
    )
    show("2. 关阀建议（故障 P6，预期建议关 P4、U3 失供）", status, body)
    assert status == 200 and body["feasible"] is True, body
    assert body["recommended_close_pipe_ids"] == ["P4"], body
    assert body["affected_sink_ids"] == ["U3"], body
    assert body["version_match"] is True, body

    # 3. 按建议直接提交隔离
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {
            "fault_pipe_id": "P6",
            "close_pipe_ids": body["recommended_close_pipe_ids"],
            "version": body["version"],
        },
    )
    show("3. 按建议提交隔离（预期成功，版本 2）", status, body)
    assert status == 200 and body["version"] == 2, body

    # 4. 对已隔离的 P6 再次建议 -> 空方案
    status, body = call(
        "POST", "/api/isolation/recommend", {"fault_pipe_id": "P6", "version": 2}
    )
    show("4. 再次建议 P6（预期空方案：两端已断开）", status, body)
    assert status == 200 and body["feasible"] is True, body
    assert body["recommended_close_pipe_ids"] == [], body

    # 5. 新故障 P5：建议不含已关闭的 P4
    status, body = call(
        "POST", "/api/isolation/recommend", {"fault_pipe_id": "P5", "version": 2}
    )
    show("5. 关阀建议（新故障 P5，预期建议关 P2、U2 失供）", status, body)
    assert status == 200 and body["feasible"] is True, body
    assert body["recommended_close_pipe_ids"] == ["P2"], body
    assert body["affected_sink_ids"] == ["U2"], body
    rec = body

    # 6. 按过期版本提交建议 -> 409
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {
            "fault_pipe_id": "P5",
            "close_pipe_ids": rec["recommended_close_pipe_ids"],
            "version": rec["version"] - 1,
        },
    )
    show("6. 过期版本提交建议（预期 409 VERSION_CONFLICT）", status, body)
    assert status == 409 and body["error"]["code"] == "VERSION_CONFLICT", body

    # 7. 按建议返回的当前版本提交 -> 成功
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {
            "fault_pipe_id": "P5",
            "close_pipe_ids": rec["recommended_close_pipe_ids"],
            "version": rec["version"],
        },
    )
    show("7. 按当前版本提交建议（预期成功，版本 3）", status, body)
    assert status == 200 and body["version"] == 3, body

    # 8. 故障管段直连水源：无可行方案
    status, body = call(
        "POST", "/api/isolation/recommend", {"fault_pipe_id": "P1", "version": 3}
    )
    show("8. 建议直连水源的故障 P1（预期 NO_FEASIBLE_PLAN）", status, body)
    assert status == 200 and body["feasible"] is False, body
    assert body["reason"]["code"] == "NO_FEASIBLE_PLAN", body

    # 9. 故障管段不存在
    status, body = call(
        "POST", "/api/isolation/recommend", {"fault_pipe_id": "PX", "version": 3}
    )
    show("9. 建议不存在的故障管段 PX（预期 FAULT_PIPE_NOT_FOUND）", status, body)
    assert status == 200 and body["feasible"] is False, body
    assert body["reason"]["code"] == "FAULT_PIPE_NOT_FOUND", body

    # 10. 建议全程只读：版本未被建议接口改变
    status, body = call("GET", "/api/network")
    show("10. 读取管网（建议只读，版本仍为 3）", status, body)
    assert body["version"] == 3, body

    print("\n全部关阀建议示例执行完成。")


if __name__ == "__main__":
    main()
