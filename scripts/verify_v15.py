# -*- coding: utf-8 -*-
"""v1.5 真机验证：异步上传任务流转 / 任务查询 API / Prometheus 指标 / 热门问题缓存（幂等）"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8766"
STAMP = str(int(time.time()))[-6:]  # 唯一后缀，可重复运行


def req(path, method="GET", data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def upload_file(filename, content, collection, token):
    """multipart/form-data 上传"""
    boundary = "----v15boundary"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"collection_name\"\r\n\r\n{collection}\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: text/plain\r\n\r\n",
    ]
    body = "".join(parts).encode("utf-8") + content.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode("utf-8")
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
               "Authorization": f"Bearer {token}"}
    r = urllib.request.Request(BASE + "/api/documents/upload", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


results = []

# 0) 登录（admin 凭据文件取初始密码）
pw = ""
for line in Path("data/admin_credentials.txt").read_text(encoding="utf-8").splitlines():
    if line.startswith("初始密码"):
        pw = line.split(":", 1)[1].strip()
        break
s, r = req("/api/auth/login", "POST", {"username": "admin", "password": pw})
TOKEN = r.get("token", "")
results.append(("管理员登录", s == 200 and bool(TOKEN)))

COLL = f"v15k{STAMP}"
s, r = req(f"/api/knowledge/collections/{COLL}", "POST", {"display_name": "v15验证库"}, token=TOKEN)
results.append(("创建测试知识库", s == 200))

# 1) 大文档异步上传（config.yaml 已临时把阈值调为 100KB，300KB 文档触发后台任务）
big_doc = "企业知识库性能优化实践指南\n" + ("GPU 服务器监控指标包括利用率、显存占用、温度与功耗。\n" * 9000)
s, r = upload_file("perf_guide.txt", big_doc, COLL, TOKEN)
results.append(("大文档返回 accepted", s == 200 and r.get("status") == "accepted"))
task_id = r.get("task_id", "")
results.append(("返回 task_id", bool(task_id)))

# 2) 任务状态轮询 → success
status, result_data = "", {}
for _ in range(120):  # 最多等 2 分钟
    s, r = req(f"/api/tasks/{task_id}", token=TOKEN)
    status = r.get("status", "")
    if status in ("success", "failed"):
        result_data = r
        break
    time.sleep(1)
results.append(("任务流转 success", status == "success"))
results.append(("任务结果含块数", isinstance(result_data.get("result", {}).get("chunks_count"), int)))

# 3) 任务列表 API（admin 可见全部；普通用户仅本人）
s, r = req("/api/tasks?page=1&size=5", token=TOKEN)
results.append(("任务列表返回", s == 200 and r.get("total", 0) >= 1))
s, r = req("/api/tasks?status=failed", token=TOKEN)
results.append(("任务按状态过滤", s == 200 and isinstance(r.get("items"), list)))
s, r = req("/api/tasks/notexist123", token=TOKEN)
results.append(("不存在的任务 404", s == 404))

# 4) 上传文件已入库
s, r = req(f"/api/documents/list/{COLL}", token=TOKEN)
results.append(("异步上传文档已入库", r.get("total_chunks", 0) > 0))

# 5) Prometheus 指标（HTTP 计数即时可见）
text = urllib.request.urlopen(BASE + "/metrics").read().decode("utf-8")
results.append(("metrics 可访问", "http_requests_total" in text))
results.append(("metrics 含上传请求计数", "/api/documents/upload" in text))

# 6) 热门问题缓存：同一问题问两次（纯 RAG、无 conversation_id）
q = f"什么是GPU服务器监控？{STAMP}"
s1, r1 = req("/api/chat/completions", "POST", {
    "message": q, "collection_name": COLL, "use_rag": True, "stream": False,
}, token=TOKEN)
results.append(("首次问答成功", s1 == 200 and bool(r1.get("answer"))))
results.append(("首次 cached=false", r1.get("cached") is False))
s2, r2 = req("/api/chat/completions", "POST", {
    "message": q, "collection_name": COLL, "use_rag": True, "stream": False,
}, token=TOKEN)
results.append(("第二次命中缓存 cached=true", r2.get("cached") is True))
results.append(("两次回答一致", r1.get("answer") == r2.get("answer")))

# 7) 上传小文档 → 缓存按库失效 → 第三次应非缓存
s, r = upload_file("invalidate_note.txt", "GPU 功耗墙与 TDP 说明。\n" * 50, COLL, TOKEN)
results.append(("小文档同步上传", s == 200 and r.get("status") == "success"))
s3, r3 = req("/api/chat/completions", "POST", {
    "message": q, "collection_name": COLL, "use_rag": True, "stream": False,
}, token=TOKEN)
results.append(("上传后缓存失效（第三次 cached=false）", r3.get("cached") is False))

# 5b) 问答已发生 → 检索耗时指标应已出现
text = urllib.request.urlopen(BASE + "/metrics").read().decode("utf-8")
results.append(("metrics 含检索耗时", "retrieval_duration_seconds" in text))
results.append(("metrics 含LLM耗时", "llm_duration_seconds" in text))

# 8) 清理：删除测试知识库
s, r = req(f"/api/knowledge/collections/{COLL}", "DELETE", token=TOKEN)
results.append(("清理测试知识库", s == 200))

print("=== v1.5 真机验证 ===")
ok = True
for name, passed in results:
    ok = ok and passed
    print(f"[{'PASS' if passed else 'FAIL'}] {name}")
print(f"\n总计 {len(results)} 项，{'全部通过' if ok else '存在失败'}")
