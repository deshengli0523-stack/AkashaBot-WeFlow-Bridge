"""
配置模块：加载 config.json，提供全局配置常量。
"""

import json
import os
import logging
import threading
from privacy import redact_log_text

# ============ 配置 ============

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))


def _runtime_path(name: str, fallback: str) -> str:
    value = os.environ.get(name, "").strip()
    return os.path.abspath(value) if value else fallback


CONFIG_FILE = _runtime_path("AKASHABOT_CONFIG_PATH", os.path.join(MODULE_DIR, "config.json"))
EXAMPLE_FILE = os.path.join(MODULE_DIR, "config.example.json")
LOG_DIR = _runtime_path("AKASHABOT_LOG_DIR", MODULE_DIR)
os.makedirs(LOG_DIR, exist_ok=True)
BRIDGE_LOG_FILE = os.path.join(LOG_DIR, "bridge.log")


def load_config():
    if not os.path.exists(CONFIG_FILE):
        if os.path.exists(EXAMPLE_FILE):
            import shutil
            shutil.copy2(EXAMPLE_FILE, CONFIG_FILE)
            print(f"[配置] 检测到 config.json 不存在，已从 config.example.json 自动创建")
        else:
            raise FileNotFoundError(f"既没有 config.json，也没有 config.example.json")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


config = load_config()

WE_FLOW_BASE_URL = config["weflow_base_url"]
ACCESS_TOKEN = config["access_token"]
ASTRBOT_ATTACHMENTS = config.get("astrbot_attachments", "")
BOT_NICKNAMES = config["bot_nicknames"]
BOT_WXID = config.get("bot_wxid", "")
UIA_FIXED_CALIBRATION = config.get("uia_fixed_calibration")
BUFFER_SECONDS = config.get("buffer_seconds", 5)
WEB_PORT = config.get("web_port", 8766)
GROUP_REPLY_MODE = config.get("group_reply_mode", "mention")  # "mention" / "all"

# AstrBot OneBot 连接配置（bridge 作为 WebSocket 客户端连 AstrBot 的 aiocqhttp 服务端）
ASTRBOT_OB_URL = config.get("astrbot_ob_url", "ws://127.0.0.1:19777")

# 图片描述配置（支持 ollama 或 openai 兼容 API）
IMAGE_CAPTION_PROVIDER = config.get("image_caption_provider", "ollama")  # "ollama" / "openai"
IMAGE_CAPTION_MODEL = config.get("image_caption_model", "llava:7b")
IMAGE_CAPTION_API_KEY = config.get("image_caption_api_key", "")
IMAGE_CAPTION_API_BASE = config.get("image_caption_api_base", "https://dashscope.aliyuncs.com/compatible-mode/v1")
IMAGE_CAPTION_PROMPT = config.get("image_caption_prompt", "请用中文简短描述这张图片的内容")

# Ollama 图片描述配置（provider=ollama 时使用）
OLLAMA_BASE_URL = config.get("ollama_base_url", "http://127.0.0.1:61000")
OLLAMA_TIMEOUT = config.get("ollama_timeout", 60)

# ============ 日志 ============

class _SensitiveValueFilter(logging.Filter):
    """Redact configured secrets and other private runtime values."""

    def __init__(self, values):
        super().__init__()
        flattened = []
        for value in values:
            candidates = value if isinstance(value, (list, tuple, set)) else (value,)
            for candidate in candidates:
                text = str(candidate).strip() if candidate is not None else ""
                if len(text) >= 4:
                    flattened.append(text)
        self._values = tuple(sorted(set(flattened), key=len, reverse=True))

    def filter(self, record):
        message = record.getMessage()
        redacted = message
        for value in self._values:
            redacted = redacted.replace(value, "[REDACTED]")
        redacted = redact_log_text(redacted, redact_paths=False)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


_privacy_filter = _SensitiveValueFilter([
    ACCESS_TOKEN, IMAGE_CAPTION_API_KEY,
    ASTRBOT_ATTACHMENTS, CONFIG_FILE, LOG_DIR,
])
_file_handler = logging.FileHandler(BRIDGE_LOG_FILE, encoding="utf-8")
_stream_handler = logging.StreamHandler()
_file_handler.addFilter(_privacy_filter)
_stream_handler.addFilter(_privacy_filter)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        _file_handler,
        _stream_handler,
    ],
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_privacy_filter)
log = logging.getLogger("ob11-bridge")
