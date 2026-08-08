# -*- coding: utf-8 -*-
"""v1.4 真机验证：密码策略 / 登录告警 / 上传+输出脱敏（UTF-8 直连，幂等）"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8766"
TOKEN = open("data/_v14_token.txt", encoding="utf-8").read().strip()
STAMP = str(int(time.time()))[-6:]  # 唯一后缀，保证可重复运行


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
    boundary = "----v14boundary"
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"collection_name\"\r\n\r\n{collection}\r\n")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: text/plain\r\n\r\n")
    body = "".join(parts).encode("utf-8") + content.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode("utf-8")
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {token}",
    }
    r = urllib.request.Request(BASE + "/api/documents/upload", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


results = []

# 0) 准备：创建测试知识库 + 唯一测试用户
COLL = f"v14sec{STAMP}"
s, r = req(f"/api/knowledge/collections/{COLL}", "POST", {"display_name": "v14脱敏验证"}, token=TOKEN)
results.append(("创建测试知识库", s == 200))

USER = f"v14u{STAMP}"

# 1) 密码策略：弱密码注册被拒
s, r = req("/api/auth/register", "POST",
           {"username": USER + "w", "password": "12345678", "display_name": "弱口令测试"},
           token=TOKEN)
results.append(("弱密码 12345678 注册被拒(400)", s == 400 and "密码" in r.get("detail", "")))
s, r = req("/api/auth/register", "POST",
           {"username": USER, "password": "V14@secure1", "display_name": "合规密码"},
           token=TOKEN)
results.append(("合规密码注册成功", s == 200))
s, r = req("/api/auth/register", "POST",
           {"username": USER + "l", "password": "abcdefgh", "display_name": "低复杂度"},
           token=TOKEN)
results.append(("低复杂度 abcdefgh 被拒(复杂度)", s == 400 and "复杂度" in r.get("detail", "")))

# 2) 登录失败告警：新用户连续 3 次错误密码 → security.alert
for _ in range(3):
    req("/api/auth/login", "POST", {"username": USER, "password": "wrongpass!"})
s, r = req("/api/audit?action=security.alert", token=TOKEN)
alerts = r.get("items", []) if isinstance(r, dict) else []
mine = [a for a in alerts if a.get("user") == USER]
results.append(("连续3次失败触发 security.alert 审计", len(mine) == 1))
results.append(("告警详情含阈值信息", bool(mine) and "3" in mine[0].get("detail", "")))

# 3) 上传链路脱敏：含手机号/身份证的文档入库后为掩码
doc = ("GPU 故障报修流程说明\n"
       "如遇服务器异常请联系值班工程师 13812345678，工单编号 110101199001011234。\n"
       "处理时限 24 小时内响应。")
s, r = upload_file("gpu_report.txt", doc, COLL, TOKEN)
results.append(("上传文档成功", s == 200))

s, r = req(f"/api/documents/list/{COLL}", token=TOKEN)
fname = (r.get("documents") or [None])[0] if isinstance(r, dict) else None
results.append(("文档已入库", bool(fname)))

s, r = req(f"/api/documents/preview/{COLL}/{fname}", token=TOKEN)
preview = json.dumps(r, ensure_ascii=False)
results.append(("入库文档手机号已掩码", "138****5678" in preview))
results.append(("入库文档身份证已掩码", "110101********1234" in preview))
results.append(("入库无明文手机号", "13812345678" not in preview))
results.append(("入库无明文身份证", "110101199001011234" not in preview))

# 4) 输出链路脱敏：提问复述手机号，回答须兜底掩码
s, r = req("/api/chat/completions", "POST", {
    "message": "我的联系电话是 13911112222，请确认记住这个号码",
    "collection_name": COLL, "use_rag": False, "stream": False,
}, token=TOKEN)
answer = (r.get("answer") or "") if isinstance(r, dict) else ""
results.append(("输出链路脱敏（13911112222 不出现）", "13911112222" not in answer))
results.append(("输出掩码存在", "139****2222" in answer))
print("[DEBUG] answer =", answer[:200])

print("=== v1.4 真机验证 ===")
ok = True
for name, passed in results:
    ok = ok and passed
    print(f"[{'PASS' if passed else 'FAIL'}] {name}")
print(f"\n总计 {len(results)} 项，{'全部通过' if ok else '存在失败'}")
