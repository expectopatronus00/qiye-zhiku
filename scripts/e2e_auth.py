"""v0.7 权限管理 e2e 验证脚本 - 输出写入 UTF-8 文件"""
import json
import urllib.request
import urllib.error
import io
import sys

BASE = "http://127.0.0.1:8766"
OUT = []


def req(path, method="GET", body=None, token=None, raw_body=False):
    url = BASE + path
    data = None
    headers = {}
    if body is not None and not raw_body:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if body is not None and raw_body:
        data = body
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8")
        try:
            return e.code, json.loads(text)
        except Exception:
            return e.code, {"detail": text[:200]}


def check(name, cond, info=""):
    OUT.append(f"[{'PASS' if cond else 'FAIL'}] {name} {info}")
    if not cond:
        OUT.append("!!! E2E 失败，提前终止")

def read_admin_password():
    with open("data/admin_credentials.txt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("初始密码"):
                return line.split(":", 1)[1].strip()
    return None

# 1. 未认证访问被拒
s, r = req("/api/knowledge/collections")
check("无 token 访问知识库列表 401", s == 401, f"-> {s}")

# 2. admin 登录
pwd = read_admin_password()
OUT.append(f"[INFO] admin 初始密码: {pwd}")
s, r = req("/api/auth/login", "POST", {"username": "admin", "password": pwd})
check("admin 登录", s == 200 and r.get("token"), f"-> {s}")
admin_token = r.get("token", "")

# 3. admin 可见知识库（存量迁移）
s, r = req("/api/knowledge/collections", token=admin_token)
names = {c["name"]: c for c in r.get("collections", [])}
check("admin 看到存量 default 库", s == 200 and "default" in names, f"-> {s} {names.keys() if isinstance(names, dict) else ''}")
check("default 库归属 admin", names.get("default", {}).get("owner") == "admin", f"-> {names.get('default', {})}")

# 4. admin 注册 user1
s, r = req("/api/auth/register", "POST", {"username": "user1", "password": "pass123", "display_name": "测试用户"}, token=admin_token)
check("admin 注册 user1", s == 200 and r.get("username") == "user1", f"-> {s} {r}")

# 5. user1 登录 + 创建知识库
s, r = req("/api/auth/login", "POST", {"username": "user1", "password": "pass123"})
check("user1 登录", s == 200 and r.get("token"), f"-> {s}")
u1_token = r.get("token", "")
s, r = req("/api/knowledge/collections/kb_u1", "POST", token=u1_token)
check("user1 创建知识库 kb_u1", s == 200 and r.get("owner") == "user1", f"-> {s} {r}")

# 6. 权限隔离
s, r = req("/api/knowledge/collections", token=u1_token)
u1_names = {c["name"] for c in r.get("collections", [])}
check("user1 只见自己的库", u1_names == {"kb_u1"}, f"-> {u1_names}")
s, r = req(f"/api/documents/list/default", token=u1_token)
check("user1 访问 default 库被拒 403", s == 403, f"-> {s}")
s, r = req("/api/knowledge/collections", token=admin_token)
check("admin 可见全部", s == 200 and len(r.get("collections", [])) >= 2, f"-> {s}")

# 7. user1 上传文档到自己的库
txt = "测试文档: CPU 温度正常区间是 40-70 摄氏度, 超过 85 度触发告警通知。".encode("utf-8")
boundary = "----e2e"
body = (
    f"--{boundary}\r\n"
    'Content-Disposition: form-data; name="file"; filename="test.txt"\r\n'
    "Content-Type: text/plain\r\n\r\n"
).encode() + txt + b"\r\n" + f"--{boundary}\r\nContent-Disposition: form-data; name=\"collection_name\"\r\n\r\nkb_u1\r\n--{boundary}--\r\n".encode()
s, r = req("/api/documents/upload", "POST", body=body, token=u1_token, raw_body=True)
# 重新带 content-type 发一次（urllib 不会自动设置，上面 req 未设置 multipart header）
import urllib.request as ur2
url = BASE + "/api/documents/upload"
hdrs = {"Authorization": f"Bearer {u1_token}", "Content-Type": f"multipart/form-data; boundary={boundary}"}
try:
    rq = ur2.Request(url, data=body, headers=hdrs, method="POST")
    with ur2.urlopen(rq, timeout=60) as resp:
        s, r = resp.status, json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    s, r = e.code, {"detail": e.read().decode("utf-8")[:300]}
check("user1 上传文档到 kb_u1", s == 200 and r.get("chunks_count", 0) > 0, f"-> {s} {r}")

# 8. 审计日志: admin 可见, user1 拒绝
s, r = req("/api/audit?size=10", token=admin_token)
check("admin 查询审计日志", s == 200 and r.get("total", 0) >= 6, f"-> {s} total={r.get('total')}")
s, r = req("/api/audit", token=u1_token)
check("user1 查询审计日志被拒 403", s == 403, f"-> {s}")

# 9. 登出后旧 token 失效
s, r = req("/api/auth/logout", "POST", token=u1_token)
check("user1 登出", s == 200, f"-> {s}")
s, r = req("/api/auth/me", token=u1_token)
check("登出后旧 token 401", s == 401, f"-> {s}")

# 10. 连续失败锁定
for i in range(5):
    s, r = req("/api/auth/login", "POST", {"username": "user1", "password": "bad"})
last = r.get("detail", "")
check("第 5 次错误密码触发锁定", "锁定" in last, f"-> {last}")

with open("e2e_result.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(OUT) + "\n")
print("DONE", len([o for o in OUT if o.startswith('[PASS]')]), "passed /",
      len([o for o in OUT if o.startswith('[FAIL]')]), "failed")
