"""敏感信息检测与脱敏 (v1.4 数据安全)

双链路脱敏：
- 上传链路：文档入库前对文本块脱敏（向量库内不落明文）
- 输出链路：LLM 回答输出前兜底脱敏（防幻觉生成）

规则集（顺序即优先级，先长后短避免嵌套误匹配）：
- 手机号   138****5678（保留前 3 后 4）
- 身份证   110101********1234（保留前 6 后 4，18 位）
- 银行卡   6222************1234（保留前 6 后 4，16-19 位）
- API Key  sk-xxx****wxyz（sk-/AKIA/ghp_ 前缀 + 通用 32+ 位高熵串，保留前 6 后 4）
- 邮箱     zh***@example.com（用户名保留前 2 后 1，域名保留）
"""
from __future__ import annotations

import re
from typing import Pattern


def _mask(text: str, head: int, tail: int) -> str:
    """保留前 head 后 tail，中间 * 填充（短串全掩）"""
    if len(text) <= head + tail:
        return "*" * len(text)
    return text[:head] + "*" * (len(text) - head - tail) + text[-tail:]


def mask_phone(match: re.Match) -> str:
    return _mask(match.group(0), 3, 4)


def mask_idcard(match: re.Match) -> str:
    return _mask(match.group(0), 6, 4)


def mask_bankcard(match: re.Match) -> str:
    return _mask(match.group(0), 6, 4)


def mask_api_key(match: re.Match) -> str:
    return _mask(match.group(0), 6, 4)


def mask_token(match: re.Match) -> str:
    return _mask(match.group(0), 4, 4)


def mask_email(match: re.Match) -> str:
    email = match.group(0)
    if "@" not in email:
        return email
    local, _, domain = email.partition("@")
    return f"{_mask(local, 2, 1)}@{domain}"


# 规则定义：(名称, 正则, 脱敏函数)。顺序即执行顺序。
SENSITIVE_RULES: list[tuple[str, Pattern, callable]] = [
    # 手机号：11 位，1 开头（前后非数字边界，避免截断 15 位身份证号等）
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), mask_phone),
    # 身份证：18 位（17 数字 + 数字/X 校验位）
    ("idcard", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), mask_idcard),
    # 银行卡：16-19 位纯数字（已被身份证规则覆盖的 18 位不会重复命中）
    ("bankcard", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), mask_bankcard),
    # API Key 常见前缀
    ("api_key", re.compile(r"(?i)(sk-[a-z0-9_\-]{16,}|AKIA[a-z0-9]{16}|ghp_[a-z0-9]{36,})"), mask_api_key),
    # 通用高熵 token：32+ 位连续字母数字
    ("token", re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{32,}(?![A-Za-z0-9])"), mask_token),
    # 邮箱
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), mask_email),
]


def mask_sensitive(text: str) -> str:
    """对文本应用全部脱敏规则（无敏感信息时原样返回）"""
    if not text:
        return ""
    for _name, pattern, repl in SENSITIVE_RULES:
        text = pattern.sub(repl, text)
    return text


def count_sensitive(text: str) -> int:
    """统计文本中敏感信息命中总数（审计/测试用）"""
    total = 0
    for _name, pattern, _repl in SENSITIVE_RULES:
        total += len(pattern.findall(text or ""))
    return total
