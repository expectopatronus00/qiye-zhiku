"""Prometheus 指标采集 (v1.5 性能与高可用)

手写文本格式输出（无第三方依赖），供 Prometheus 抓取。

指标：
- http_requests_total{method,path,status}       请求计数（QPS 素材）
- http_request_duration_seconds                 请求延迟直方图
- retrieval_duration_seconds                    检索耗时直方图（向量+BM25+融合）
- llm_duration_seconds                          LLM 推理耗时直方图（chat/stream）

用法：
    from app.core.metrics import record_request, record_duration, render_metrics
    record_request(method, path, status, seconds)   # 中间件内调用
    record_duration("retrieval", seconds)           # 业务埋点
    render_metrics()                                # /metrics 端点输出
"""
from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict

# 直方图 bucket 边界（秒）
_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

_lock = threading.Lock()
# (method, path, status) -> count
_http_total: Counter = Counter()
# (method, path) -> {bucket_i: count}
_http_buckets: defaultdict = defaultdict(Counter)
_http_sum: defaultdict = defaultdict(float)
_http_count: defaultdict = defaultdict(int)
# name -> 直方图（"retrieval" / "llm"）
_dur_buckets: defaultdict = defaultdict(Counter)
_dur_sum: defaultdict = defaultdict(float)
_dur_count: defaultdict = defaultdict(int)

_START_TIME = time.time()


def record_request(method: str, path: str, status: int, seconds: float) -> None:
    """请求中间件埋点"""
    with _lock:
        _http_total[(method, path, status)] += 1
        key = (method, path)
        _http_buckets[key][_bucket_index(seconds)] += 1
        _http_sum[key] += seconds
        _http_count[key] += 1


def record_duration(name: str, seconds: float) -> None:
    """业务耗时埋点（name: retrieval / llm）"""
    with _lock:
        _dur_buckets[name][_bucket_index(seconds)] += 1
        _dur_sum[name] += seconds
        _dur_count[name] += 1


def _bucket_index(value: float) -> int:
    for i, b in enumerate(_BUCKETS):
        if value <= b:
            return i
    return len(_BUCKETS)


def render_metrics() -> str:
    """渲染 Prometheus 文本格式"""
    with _lock:
        lines = [
            "# HELP qiye_zhiku_uptime_seconds 服务运行时长",
            "# TYPE qiye_zhiku_uptime_seconds gauge",
            f"qiye_zhiku_uptime_seconds {time.time() - _START_TIME:.0f}",
            "",
            "# HELP http_requests_total 请求总数",
            "# TYPE http_requests_total counter",
        ]
        for (method, path, status), cnt in sorted(_http_total.items()):
            lines.append(
                f'http_requests_total{{method="{method}",path="{path}",status="{status}"}} {cnt}'
            )
        lines += ["", "# HELP http_request_duration_seconds 请求延迟",
                  "# TYPE http_request_duration_seconds histogram"]
        for (method, path), _ in sorted(_http_buckets.items()):
            base = f'{{method="{method}",path="{path}"'
            for i, b in enumerate(_BUCKETS):
                lines.append(f"http_request_duration_seconds_bucket{base},le=\"{b:g}\"}} "
                             f"{_http_buckets[(method, path)][i]}")
            lines.append(f"http_request_duration_seconds_bucket{base},le=\"+Inf\"}} "
                         f"{_http_count[(method, path)]}")
            lines.append(f"http_request_duration_seconds_sum{base}}} {_http_sum[(method, path)]:.6f}")
            lines.append(f"http_request_duration_seconds_count{base}}} {_http_count[(method, path)]}")
        for name in ("retrieval", "llm"):
            if _dur_count.get(name, 0) == 0:
                continue
            lines += ["",
                      f"# HELP {name}_duration_seconds {name} 耗时",
                      f"# TYPE {name}_duration_seconds histogram"]
            base = f'{{name="{name}"'
            for i, b in enumerate(_BUCKETS):
                lines.append(f"{name}_duration_seconds_bucket{base},le=\"{b:g}\"}} "
                             f"{_dur_buckets[name][i]}")
            lines.append(f"{name}_duration_seconds_bucket{base},le=\"+Inf\"}} {_dur_count[name]}")
            lines.append(f"{name}_duration_seconds_sum{base}}} {_dur_sum[name]:.6f}")
            lines.append(f"{name}_duration_seconds_count{base}}} {_dur_count[name]}")
        return "\n".join(lines) + "\n"
