"""Webhook 通知 (v1.6) - 飞书/钉钉自定义机器人推送

- 支持飞书(https://open.feishu.cn/open-apis/bot/v2/hook/)与
  钉钉(https://oapi.dingtalk.com/robot/send?access_token=)自定义机器人，
  每类最多多个 URL（逗号分隔）
- 事件: document.uploaded 上传完成 / task.failed 后台任务失败 /
  security.alert 安全告警 / feedback.submitted 用户反馈
- 实现: 后台 daemon 线程发送 + 5s 超时 + 单次重试，失败仅记日志——
  完全不阻塞上传/问答主流程（同步或异步上下文均可安全调用）
"""
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

FEISHU_TMPL = "【企业智库】{title}\n{body}"
DINGTALK_TMPL = "【企业智库】{title}\n{body}"


class WebhookManager:
    """飞书/钉钉机器人推送器"""

    def __init__(self, feishu_urls: str = "", dingtalk_urls: str = "",
                 enabled: bool = True, timeout: float = 5.0):
        self.enabled = enabled
        self.timeout = timeout
        self.feishu_urls = self._split(feishu_urls)
        self.dingtalk_urls = self._split(dingtalk_urls)
        self._client = None
        self._lock = threading.Lock()

    @staticmethod
    def _split(urls: str) -> list[str]:
        return [u.strip() for u in (urls or "").split(",") if u.strip()]

    def configure(self, feishu_urls: str = "", dingtalk_urls: str = "",
                  enabled: Optional[bool] = None):
        """热更新（管理台改配置后调用）"""
        self.enabled = enabled if enabled is not None else self.enabled
        self.feishu_urls = self._split(feishu_urls)
        self.dingtalk_urls = self._split(dingtalk_urls)
        with self._lock:
            self._client = None  # 重置客户端复用新 URL

    def _get_client(self):
        """懒加载 httpx 同步客户端"""
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                try:
                    import httpx
                    self._client = httpx.Client(timeout=self.timeout)
                except Exception:
                    logger.warning("httpx 不可用，webhook 通知停用")
                    return None
        return self._client

    # ------------------------------------------------------------ 发送
    def _post(self, url: str, payload: dict) -> bool:
        client = self._get_client()
        if client is None:
            return False
        for attempt in range(2):  # 单次重试
            try:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                # 钉钉/飞书成功响应体含 code/StatusMessage 字段
                data = resp.json()
                if data.get("code") not in (None, 0) or data.get("StatusCode") not in (None, 0):
                    logger.warning("webhook 响应异常: %s", data)
                    return False
                return True
            except Exception:
                if attempt == 0:
                    logger.warning("webhook 发送失败(第1次)，重试中: %s", url)
                else:
                    logger.warning("webhook 发送失败(重试后放弃): %s", url, exc_info=True)
        return False

    def _dispatch(self, title: str, body: str):
        for url in self.feishu_urls:
            self._post(url, {"msg_type": "text",
                             "content": {"text": FEISHU_TMPL.format(title=title, body=body)}})
        for url in self.dingtalk_urls:
            self._post(url, {"msgtype": "text",
                             "text": {"content": DINGTALK_TMPL.format(title=title, body=body)}})

    def fire(self, event: str, title: str, body: str) -> bool:
        """触发事件通知（后台线程发送，立即返回不阻塞）"""
        if not self.enabled:
            return False
        if not self.feishu_urls and not self.dingtalk_urls:
            return False
        t = threading.Thread(target=self._dispatch, args=(title, body),
                             daemon=True, name=f"webhook-{event}")
        t.start()
        return True


# 全局单例（URL 由配置热更新注入）
_webhook_manager: Optional[WebhookManager] = None
_webhook_lock = threading.Lock()


def get_webhook_manager() -> WebhookManager:
    """按全局配置获取/重建管理器（URL 热更新生效）"""
    global _webhook_manager
    from app.core.config import settings
    wc = settings.webhook
    with _webhook_lock:
        if _webhook_manager is None:
            _webhook_manager = WebhookManager(feishu_urls=wc.feishu_urls,
                                              dingtalk_urls=wc.dingtalk_urls,
                                              enabled=wc.enabled)
        else:
            _webhook_manager.configure(feishu_urls=wc.feishu_urls,
                                       dingtalk_urls=wc.dingtalk_urls,
                                       enabled=wc.enabled)
        return _webhook_manager


def fire_event(event: str, title: str, body: str) -> bool:
    """便捷入口：按事件开关决定是否发送（未配置 URL 自动跳过）"""
    from app.core.config import settings
    wc = settings.webhook
    if not wc.enabled:
        return False
    if event == "document.uploaded" and not wc.notify_upload:
        return False
    if event == "task.failed" and not wc.notify_task_failed:
        return False
    if event == "security.alert" and not wc.notify_security_alert:
        return False
    if event == "feedback.submitted" and not wc.notify_feedback:
        return False
    return get_webhook_manager().fire(event, title, body)
