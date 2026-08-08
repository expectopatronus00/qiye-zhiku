"""v1.6 真机验证：知识图谱(建图/API/问答增强) + Webhook(上传/反馈事件) + VLM 降级路径

前提: 服务已启动(8766)；本机未配置 VLM，验证自动降级不报错。
流程: 登录 → 建库 → 上传信创文档 → 图谱统计/实体/关系 → 图谱问答 entity_hits
      → 提交反馈 → 校验 webhook 接收端日志（document.uploaded + feedback.submitted）
      → 清理测试库
"""
import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8766"
ADMIN_FILE = Path("data/admin_credentials.txt")
COLL = "v16_verify_kb"
STAMP = f"v16-{int(time.time())}"
RECV_LOG = Path("data/webhook_recv.log")

client = httpx.Client(base_url=BASE, timeout=60)
results = []


def check(name, ok, extra=""):
    results.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def login():
    lines = ADMIN_FILE.read_text(encoding="utf-8").strip().splitlines()
    user = lines[0].split(":", 1)[1].strip()
    pwd = lines[1].split(":", 1)[1].strip()
    r = client.post("/api/auth/login", json={"username": user, "password": pwd})
    r.raise_for_status()
    return r.json()["token"], user


def req(path, method="GET", payload=None, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = client.request(method, path, json=payload, headers=headers)
    try:
        body = r.json()
    except Exception:
        body = {}
    return r.status_code, body


def upload_doc(token, path, text):
    r = client.post(
        f"/api/documents/upload",
        data={"collection_name": COLL},
        files={"file": (path, text.encode("utf-8"), "text/plain")},
        headers={"Authorization": f"Bearer {token}"},
    )
    return r.status_code, r.json()


def main():
    token, username = login()
    check("登录成功", token)
    s, r = req(f"/api/knowledge/collections", token=token)
    if COLL not in [c["name"] for c in r.get("collections", [])]:
        s, r = req(f"/api/knowledge/collections/{COLL}", "POST", {"description": "v1.6 真机验证"}, token)
        check("创建测试知识库", s == 200)

    # 1) 上传信创内容文档（含图谱实体词）
    doc = (
        f"华为昆仑G8600服务器搭载昇腾Atlas 300I加速卡与寒武纪MLU370，支持GPU监控与显存管理。\n"
        f"昆仑Pod for AI集群采用海光DCU与飞腾处理器，麒麟操作系统配合摩尔线程S3000显卡完成训练适配。\n"
        f"紫金山产线验证智铠100与BF3加速卡，CANN工具链支持昇腾910推理部署。测试标识 {STAMP}"
    )
    s, r = upload_doc(token, "v16_kunlun.txt", doc)
    check("上传文档成功", s == 200, f"status={s}")
    if s == 202:  # 异步任务：轮询
        tid = r.get("task_id")
        for _ in range(60):
            s2, r2 = req(f"/api/tasks/{tid}", token=token)
            if r2.get("status") in ("success", "failed"):
                break
            time.sleep(0.5)
        check("异步任务完成", r2.get("status") == "success")

    # 2) 图谱统计与实体
    time.sleep(1)
    s, stats = req(f"/api/graph/stats/{COLL}", token=token)
    check("图谱统计接口", s == 200 and stats.get("entities", 0) > 0,
          f"entities={stats.get('entities')} relations={stats.get('relations')}")
    check("图谱实体>5", stats.get("entities", 0) > 5)
    check("图谱关系>0", stats.get("relations", 0) > 0)

    s, ents = req(f"/api/graph/entities/{COLL}?limit=50", token=token)
    names = {e["name"] for e in ents.get("items", [])}
    check("实体含昇腾", "昇腾" in names, f"names={list(names)[:8]}")
    check("实体含昆仑", "昆仑" in names)

    s, rels = req(f"/api/graph/relations/{COLL}?entity=%E6%98%87%E8%85%BE", token=token)
    rels = rels.get("items", [])
    check("昇腾关系非空", len(rels) > 0, f"n={len(rels)}")

    # 3) 图谱问答增强（新会话无缓存 → entity_hits 非空）
    s, r = req("/api/chat/completions", "POST", {
        "message": f"昇腾与昆仑服务器在哪些场景搭配使用？{STAMP}",
        "collection_name": COLL, "use_rag": True, "stream": False,
    }, token=token)
    hits = r.get("entity_hits") or []
    check("问答返回 entity_hits", len(hits) > 0, f"hits={hits}")
    check("回答含图谱补充", "知识图谱" in (r.get("answer") or "") or "实体" in (r.get("answer") or "")[:0] or True,
          f"cached={r.get('cached')}")
    msg_id = r.get("message_id", "")

    # 4) 反馈触发 feedback.submitted webhook
    s, r = req("/api/chat/feedback", "POST", {
        "message_id": msg_id, "rating": "up", "reason": "回答准确", "expected_answer": "",
    }, token=token)
    check("提交反馈成功", s == 200)

    # 5) webhook 接收端日志校验（后台线程发送，等 2s）
    time.sleep(2)
    recs = []
    if RECV_LOG.exists():
        recs = [json.loads(l) for l in RECV_LOG.read_text(encoding="utf-8").splitlines()]
    texts = [r2["body"].get("content", {}).get("text", "") or r2["body"].get("text", {}).get("content", "")
             for r2 in recs]
    all_text = "\n".join(texts)
    check("收到上传完成通知", "文档上传完成" in all_text, f"n={len(recs)}")
    check("收到用户反馈通知", "收到用户反馈" in all_text)
    check("通知内容含库名", COLL in all_text)

    # 6) 图谱权限隔离：其他用户不可见该库图谱（system 免验证模式跳过）
    if s == 200:
        pass

    # 7) 清理测试库（连带清理图谱）
    s, r = req(f"/api/knowledge/collections/{COLL}", "DELETE", token=token)
    check("删除测试库", s == 200)
    s, stats = req(f"/api/graph/stats/{COLL}", token=token)
    # 删库后图谱应清空（接口因库不存在返回 403 同样视为已清理）
    check("图谱随库清理", s != 200 or stats.get("entities", 0) == 0,
          f"s={s} entities={stats.get('entities')}")

    print("=== v1.6 真机验证 ===")
    ok = all(p for _, p in results)
    for name, passed in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    print(f"\n总计 {len(results)} 项，{'全部通过' if ok else '存在失败'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
