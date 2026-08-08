"""多模态 VLM 图表理解 (v1.6 信创路线)

- 走 OpenAI 兼容协议 /v1/chat/completions：昇腾 CANN(vLLM-Ascend)/寒武纪/摩尔线程
  部署的 Qwen2.5-VL 等本地 VLM 可直接对接
- base_url 为空（未配置 VLM）→ describe_image 返回 None，调用方自然降级纯 OCR
- 图片 base64 内联传输；失败/超时静默降级，不影响文档入库主流程
"""
import base64
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# 图表描述 Prompt：要求结构化输出（主题/坐标轴/趋势/关键数值），便于中文检索命中
CAPTION_PROMPT = (
    "你是一个专业的数据图表解读助手。请用简洁中文描述这张图片（图表/示意图/截图）："
    "1) 图片主题与类型（如 GPU 利用率折线图、故障告警表）；"
    "2) 横轴/纵轴或关键字段含义；"
    "3) 关键数据点与总体趋势；"
    "4) 若含异常/告警信息请明确指出。控制在 150 字以内。"
)


class VLMCaptioner:
    """OpenAI 兼容 VLM 调用器，未配置时全部返回 None（降级 OCR）"""

    def __init__(self, base_url: str = "", model: str = "qwen2.5-vl:7b",
                 api_key: str = "", timeout: float = 20.0):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._client = None
        self._lock = threading.Lock()
        self._failed = False

    def _get_client(self):
        """懒加载 httpx 客户端；未配置/导入失败/曾失败 → None"""
        if self._failed:
            return None
        with self._lock:
            if self._client is not None:
                return self._client
            if not self.base_url:
                self._failed = True
                return None
            try:
                import httpx
                headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
                self._client = httpx.Client(base_url=self.base_url, headers=headers,
                                            timeout=self.timeout)
            except Exception:
                logger.warning("VLM 客户端初始化失败，降级 OCR", exc_info=True)
                self._failed = True
        return self._client

    def describe_image(self, img_bytes: bytes, mime: str = "image/png",
                       context: str = "") -> Optional[str]:
        """生成图表中文描述；失败/未配置返回 None"""
        client = self._get_client()
        if client is None:
            return None
        if not img_bytes:
            return None
        try:
            b64 = base64.b64encode(img_bytes).decode("ascii")
            data_url = f"data:{mime};base64,{b64}"
            text = CAPTION_PROMPT
            if context:
                text += f"\n（该图位于文档章节：{context}）"
            resp = client.post("/chat/completions", json={
                "model": self.model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
                "temperature": 0.2,
                "max_tokens": 300,
            })
            resp.raise_for_status()
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            content = (content or "").strip()
            return content or None
        except Exception:
            logger.warning("VLM 图表描述失败，降级 OCR", exc_info=True)
            return None


# 全局实例（config 热更新后由调用方重新构造或直接使用全局 settings 读取）
_vlm_captioner: Optional[VLMCaptioner] = None
_vlm_lock = threading.Lock()


def get_captioner() -> VLMCaptioner:
    """按当前全局配置获取/重建 VLM 调用器（base_url 热更新生效）"""
    global _vlm_captioner
    from app.core.config import settings
    vc = settings.vision
    with _vlm_lock:
        if _vlm_captioner is None or _vlm_captioner.base_url != vc.base_url:
            _vlm_captioner = VLMCaptioner(base_url=vc.base_url, model=vc.model,
                                          api_key=vc.api_key)
        return _vlm_captioner
