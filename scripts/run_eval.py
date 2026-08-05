# -*- coding: utf-8 -*-
"""评估运行器 - RAG 系统质量评估 CLI (Day 6)

用法:
    python scripts/run_eval.py                      # 评估 eval/dataset.json
    python scripts/run_eval.py --limit 3            # 只评估前 3 题（快速验证）
    python scripts/run_eval.py --collection my_kb   # 指定知识库
    python scripts/run_eval.py --min-faithfulness 0.6   # 覆盖质量门禁阈值

流程: 检索 → RAG 回答 → 三项指标(忠实度/相关性/召回率) → 汇总报告
退出码: 0 通过质量门禁 / 1 未达标（供 CI 集成）
"""
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings          # noqa: E402
from app.core.evaluator import RAGASEvaluator  # noqa: E402
from app.core.llm import LLMService            # noqa: E402
from app.core.retriever import Retriever       # noqa: E402
from app.routers.chat import RAG_SYSTEM_PROMPT  # noqa: E402


def build_answer_messages(query: str, docs: list[dict]) -> list[dict]:
    """与线上 chat 接口完全一致的 RAG 消息组装"""
    context = "\n\n---\n\n".join(
        f"[来源: {doc.get('metadata', {}).get('filename', '未知')}]\n{doc['content']}"
        for doc in docs
    )
    return [
        {"role": "system", "content": RAG_SYSTEM_PROMPT.format(context=context)},
        {"role": "user", "content": query},
    ]


async def evaluate_one(evaluator, llm, retriever, item: dict, collection: str) -> dict:
    """检索 + 回答 + 评估单个样本"""
    question = item["question"]
    golden = item.get("golden_answer", "")

    # 1. 检索
    docs = await retriever.retrieve(question)
    contexts = [
        {"content": doc["content"], "metadata": doc.get("metadata", {})}
        for doc in docs
    ]

    # 2. 回答（复用线上 RAG 提示词）
    messages = build_answer_messages(question, docs)
    answer = await llm.chat(messages)

    # 3. 评估
    result = await evaluator.evaluate_item(
        question=question, golden_answer=golden,
        contexts=contexts, answer=answer,
    )
    result["golden_answer"] = golden
    result["retrieved_filenames"] = sorted({
        c["metadata"].get("filename", "未知") for c in contexts
    })
    return result


async def run_eval(args) -> dict:
    dataset_path = Path(args.dataset)
    with open(dataset_path, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    items = dataset.get("items", [])
    if not items:
        raise ValueError(f"评测集为空: {dataset_path}")

    collection = args.collection or dataset.get("collection", "default")
    if args.limit:
        items = items[: args.limit]

    llm = LLMService()
    evaluator = RAGASEvaluator(llm=llm)
    retriever = Retriever(collection_name=collection)

    thresholds = {
        "faithfulness": args.min_faithfulness,
        "answer_relevancy": args.min_answer_relevancy,
        "context_recall": args.min_context_recall,
    }

    results = []
    for i, item in enumerate(items, 1):
        print(f"[{i}/{len(items)}] {item['question'][:30]}...", flush=True)
        t0 = time.time()
        result = await evaluate_one(evaluator, llm, retriever, item, collection)
        result["elapsed_sec"] = round(time.time() - t0, 1)
        results.append(result)
        print(
            f"    faith={result['faithfulness']['score']:.3f} "
            f"relev={result['answer_relevancy']['score']:.3f} "
            f"recall={result['context_recall']['score']:.3f} "
            f"({result['elapsed_sec']}s)", flush=True,
        )

    # 汇总
    summary = {}
    for metric in ("faithfulness", "answer_relevancy", "context_recall"):
        scores = [r[metric]["score"] for r in results]
        summary[metric] = round(sum(scores) / len(scores), 4) if scores else 0.0

    gate_pass = all(summary[m] >= thresholds[m] for m in thresholds)

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dataset": str(dataset_path),
        "collection": collection,
        "judge_model": evaluator.judge_model,
        "answer_model": llm.model,
        "items_count": len(items),
        "thresholds": thresholds,
        "gate_pass": gate_pass,
        "summary": summary,
        "items": results,
    }

    # 输出报告文件
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"report_{stamp}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"report saved: {json_path}", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description="RAG 系统质量评估 (RAGAS 方法论, 本地裁判)")
    parser.add_argument("--dataset", default=str(PROJECT_ROOT / "eval" / "dataset.json"))
    parser.add_argument("--collection", default="", help="知识库名，默认取评测集配置")
    parser.add_argument("--limit", type=int, default=0, help="只评估前 N 题")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "data" / "eval_reports"))
    parser.add_argument("--min-faithfulness", type=float, default=settings.eval.min_faithfulness)
    parser.add_argument("--min-answer-relevancy", type=float, default=settings.eval.min_answer_relevancy)
    parser.add_argument("--min-context-recall", type=float, default=settings.eval.min_context_recall)
    args = parser.parse_args()

    if not settings.eval.enabled:
        print("eval.enabled=false, skipped", flush=True)
        sys.exit(0)

    report = asyncio.run(run_eval(args))

    s = report["summary"]
    print("=" * 50, flush=True)
    print(f"faithfulness      = {s['faithfulness']:.4f} (min {args.min_faithfulness})", flush=True)
    print(f"answer_relevancy  = {s['answer_relevancy']:.4f} (min {args.min_answer_relevancy})", flush=True)
    print(f"context_recall    = {s['context_recall']:.4f} (min {args.min_context_recall})", flush=True)
    print(f"gate: {'PASS' if report['gate_pass'] else 'FAIL'}", flush=True)
    sys.exit(0 if report["gate_pass"] else 1)


if __name__ == "__main__":
    main()
