"""
Message sender implementations.

Supported modes:
- weflow_api: send through WeFlow HTTP API
- uia: original automatic UIA sender
- uia_fixed: fixed foreground WeChat UI sender
"""

import logging
import os

import requests

from config import ACCESS_TOKEN, SEND_METHOD, UIA_FIXED_CONFIG, WE_FLOW_SEND_API
from uia_fixed_sender import UiaFixedSender
from uia_sender import UiaSender

log = logging.getLogger("ob11-bridge")


class BaseSender:
    def send_text(self, contact: str, text: str) -> bool:
        raise NotImplementedError

    def send_image(self, contact: str, image_path: str) -> bool:
        raise NotImplementedError


class WeFlowApiSender(BaseSender):
    def __init__(self, api_url: str, access_token: str):
        self.api_url = api_url
        self.access_token = access_token

    def send_text(self, contact: str, text: str) -> bool:
        try:
            resp = requests.post(
                self.api_url,
                json={"to": contact, "content": text, "type": "text"},
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=10,
            )
            if resp.status_code == 200:
                log.info("[WeFlowSender] text sent: characters=%s", len(text))
                return True
            log.error("[WeFlowSender] send failed: HTTP %s", resp.status_code)
            return False
        except Exception:
            log.error("[WeFlowSender] text request failed")
            return False

    def send_image(self, contact: str, image_path: str) -> bool:
        try:
            with open(image_path, "rb") as f:
                resp = requests.post(
                    self.api_url,
                    data={"to": contact, "type": "image"},
                    files={"image": f},
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    timeout=30,
                )
            if resp.status_code in (200, 201):
                log.info("[WeFlowSender] image sent")
                return True
            log.error("[WeFlowSender] image send failed: HTTP %s", resp.status_code)
            return False
        except Exception:
            log.error("[WeFlowSender] image request failed")
            return False


def create_sender() -> BaseSender:
    if SEND_METHOD == "weflow_api":
        log.info("Using WeFlow API sender")
        return WeFlowApiSender(WE_FLOW_SEND_API, ACCESS_TOKEN)
    if SEND_METHOD == "uia_fixed":
        log.info("Using fixed foreground WeChat UI sender")
        return UiaFixedSender(**UIA_FIXED_CONFIG)
    log.info("Using UIA sender")
    return UiaSender()
