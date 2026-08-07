"""黄金评测集一键回归 (v1.2 检索效果工程)

用法（在项目根目录）:
  C:/Users/18821/Python312/python.exe eval/run_regression.py
  C:/Users/18821/Python312/python.exe eval/run_regression.py --vector-only   # 对比纯向量
  C:/Users/18821/Python312/python.exe eval/run_regression.py --both          # 混合 vs 纯向量 对比
  C:/Users/18821/Python312/python.exe eval/run_regression.py --collect-feedback  # 合并用户反馈回流集

指标: hit@5（正确文档进 top5 的比例）、MRR（倒排平均秩）、top1 命中率。
基线: 首次运行写入 data/eval_reports/baseline.json，再次运行输出与基线差值（提升/下降）。
依赖: Ollama 运行中（embedding nomic-embed-text）、向量库已有数据。
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# 兼容直接执行（脚本在 eval/ 下，项目根为上级目录）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.core.retriever import Retriever  # noqa: E402

REPORT_DIR = ROOT / "data" / "eval_reports"


def load_dataset(with_feedback: bool) -> list[dict]:
    """加载黄金评测集（+ 可选用户反馈回流）"""
    ds_path = ROOT / "eval" / "dataset.json"
    with open(ds_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = list(data.get("items", []))
    if with_feedback:
        # 从安全库读取回流条目（与服务同一 SQLite）
        import sqlite3
        db = Path(settings.security.db_path)
        if db.exists():
            conn = sqlite3.connect(db)
            rows = conn.execute(
                "SELECT question, expected_answer FROM feedback "
                "WHERE rating='down' AND expected_answer != '' ORDER BY id DESC LIMIT 500"
            ).fetchall()
            conn.close()
            for q, ans in rows:
                if q:
                    items.append({"question": q, "golden_answer": ans,
                                  "source_doc": "(feedback)", "origin": "user_feedback"})
            print(f"[info] 合并用户反馈回流 {len(rows)} 条")
    return items


def _hit(metadata: dict, source_doc: str) -> bool:
    """命中判定：检索结果文档名与期望文档匹配（含中英文文件名）"""
    if not source_doc or source_doc.startswith("("):
        return True  # 无来源锚点（如反馈回流）不参与命中判定，仅统计
    fn = (metadata or {}).get("filename", "") or ""
    return source_doc.lower() in fn.lower() or fn.lower() in source_doc.lower()


async def run_mode(mode: str, items: list[dict]) -> dict:
    """跑一种检索模式，返回指标 + 明细"""
    settings.retrieval.hybrid_search = mode == "hybrid"
    retriever = Retriever(collection_name="default")
    hits, mrr_sum, top1 = 0, 0.0, 0
    judged, total = 0, len(items)
    details = []

    for i, item in enumerate(items, 1):
        docs = await retriever.retrieve(item["question"])
        rank = None
        for r, doc in enumerate(docs[:5]):
            if _hit(doc.get("metadata", {}), item.get("source_doc", "")):
                rank = r + 1
                break
        if item.get("source_doc") and not item["source_doc"].startswith("("):
            judged += 1
        if rank:
            hits += 1
            mrr_sum += 1.0 / rank
            if rank == 1:
                top1 += 1
        details.append({
            "question": item["question"][:60],
            "hit_rank": rank,
            "fusion": retriever.last_debug.get("fusion") if retriever.last_debug else "",
            "elapsed_ms": retriever.last_debug.get("elapsed_ms") if retriever.last_debug else 0,
        })
        if i % 5 == 0 or i == total:
            print(f"  [{mode}] {i}/{total} 进行中...")

    return {
        "mode": mode,
        "total": total,
        "hit@5": round(hits / total, 4) if total else 0,
        "mrr": round(mrr_sum / total, 4) if total else 0,
        "top1": round(top1 / total, 4) if total else 0,
        "hits": hits,
        "top1_count": top1,
        "details": details,
    }


def load_baseline() -> dict:
    path = REPORT_DIR / "baseline.json"
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_baseline(result: dict):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORT_DIR / "baseline.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def fmt_metric(name: str, cur: float, base: float | None) -> str:
    if base is None:
        return f"{cur:.4f}"
    diff = cur - base
    arrow = "▲" if diff > 0.0005 else ("▼" if diff < -0.0005 else "=")
    return f"{cur:.4f} ({arrow}{abs(diff):.4f})"


async def main():
    parser = argparse.ArgumentParser(description="黄金评测集一键回归")
    parser.add_argument("--mode", choices=["hybrid", "vector", "both"], default="hybrid",
                        help="检索模式: hybrid=RRF混合(默认) vector=纯向量 both=对比")
    parser.add_argument("--collect-feedback", action="store_true",
                        help="合并用户反馈回流条目")
    args = parser.parse_args()

    items = load_dataset(args.collect_feedback)
    if not items:
        print("[err] 评测集为空，先上传文档并生成 eval/dataset.json")
        return 1

    print(f"[info] 评测集 {len(items)} 条，开始检索回归...\n")
    baseline = load_baseline()

    if args.mode in ("hybrid", "both"):
        result = await run_mode("hybrid", items)
        b = baseline.get("hybrid")
        print(f"[hybrid]   hit@5={fmt_metric('h', result['hit@5'], b['hit@5'] if b else None)}  "
              f"MRR={fmt_metric('m', result['mrr'], b['mrr'] if b else None)}  "
              f"top1={fmt_metric('t', result['top1'], b['top1'] if b else None)}")
        save_baseline({"hybrid": {k: result[k] for k in ("hit@5", "mrr", "top1")}})
        (REPORT_DIR / f"regression_hybrid_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.mode in ("vector", "both"):
        result = await run_mode("vector", items)
        b = baseline.get("vector")
        print(f"[vector]   hit@5={fmt_metric('h', result['hit@5'], b['hit@5'] if b else None)}  "
              f"MRR={fmt_metric('m', result['mrr'], b['mrr'] if b else None)}  "
              f"top1={fmt_metric('t', result['top1'], b['top1'] if b else None)}")
        if args.mode == "both":
            base = baseline.get("vector")
            save_baseline({"vector": {k: result[k] for k in ("hit@5", "mrr", "top1")},
                           **({"hybrid": baseline.get("hybrid")} if baseline.get("hybrid") else {})})
        else:
            save_baseline({"vector": {k: result[k] for k in ("hit@5", "mrr", "top1")}})
        (REPORT_DIR / f"regression_vector_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.mode == "both":
        v = baseline.get("vector") or {}
        h = baseline.get("hybrid") or {}
        if v and h:
            print("\n[对比] 混合 vs 纯向量:")
            for k in ("hit@5", "mrr", "top1"):
                d = h.get(k, 0) - v.get(k, 0)
                print(f"  {k}: {'▲' if d > 0.0005 else ('▼' if d < -0.0005 else '=')} {abs(d):.4f}")

    if not baseline:
        print("\n[info] 已保存本次结果为基线 (data/eval_reports/baseline.json)，下次运行自动对比")
    print(f"\n[info] 明细报告已写入 data/eval_reports/")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
