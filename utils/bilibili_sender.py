import asyncio
import json
import re
import threading
import time
from collections import deque
from pathlib import Path

import requests
from bilibili_api import Credential, live
from bilibili_api.utils.danmaku import Danmaku

from utils.my_log import logger


class BilibiliLiveSender:
    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self._room = None
        self._sender_username = None
        self._sender_uid = self._parse_cookie_data(
            self.config.get("bilibili", "cookie") or "", "DedeUserID"
        )
        self._recent_sent = deque(maxlen=20)
        self._recent_sent_file = (
            Path(__file__).resolve().parent.parent / "cache" / "bilibili_recent_sent.json"
        )
        self._recent_sent_file.parent.mkdir(parents=True, exist_ok=True)

    def enabled(self):
        return bool(self.config.get("bilibili_send_reply", "enable"))

    def _build_room(self):
        cookie = self.config.get("bilibili", "cookie") or ""
        room_id = self.config.get("room_display_id")
        if not cookie or not room_id:
            raise ValueError("缺少B站发送弹幕所需的 cookie 或直播间号")

        credential = Credential(
            sessdata=self._parse_cookie_data(cookie, "SESSDATA"),
            bili_jct=self._parse_cookie_data(cookie, "bili_jct"),
            buvid3=self._parse_cookie_data(cookie, "buvid3"),
            dedeuserid=self._parse_cookie_data(cookie, "DedeUserID"),
            ac_time_value=self.config.get("bilibili", "ac_time_value") or None,
        )
        return live.LiveRoom(int(room_id), credential=credential)

    def _get_room(self):
        with self._lock:
            if self._room is None:
                self._room = self._build_room()
            return self._room

    def format_reply(self, username, content):
        template_enabled = bool(self.config.get("reply_template", "enable"))
        if template_enabled:
            variables = {
                "username": (username or "")[
                    : int(self.config.get("reply_template", "username_max_len") or 10)
                ],
                "data": content,
            }
            templates = self.config.get("reply_template", "copywriting") or ["{data}"]
            template = templates[0] if templates else "{data}"
            try:
                content = template.format(**variables)
            except Exception:
                pass

        max_len = int(self.config.get("bilibili_send_reply", "max_len") or 80)
        return (content or "").strip()[:max_len]

    def _webui_callback(self, content, success=True, username="公屏"):
        try:
            webui_ip = (
                "127.0.0.1"
                if self.config.get("webui", "ip") == "0.0.0.0"
                else self.config.get("webui", "ip")
            )
            content_type = "public_reply_log" if success else "public_reply_error"
            payload = {
                "type": "llm",
                "data": {
                    "type": "B站公屏" if success else "B站公屏失败",
                    "username": username,
                    "content_type": content_type,
                    "content": content,
                    "silent": True,
                    "source": "bilibili_sender",
                    "timestamp": __import__("datetime").datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                },
            }
            requests.post(
                f"http://{webui_ip}:{self.config.get('webui', 'port')}/callback",
                json=payload,
                timeout=10,
            )
        except Exception as e:
            logger.debug(f"回传 WebUI 公屏日志失败: {e}")

    def _parse_cookie_data(self, data_str, field_name):
        for pair in data_str.split(";"):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            if key.strip() == field_name:
                return value.strip()
        return None

    def _normalize_content(self, content):
        text = (content or "").strip()
        text = re.sub(r"\[\[[^\]]+\]\]", "", text)
        text = text.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
        text = re.sub(r"\s+", "", text)
        return text

    async def _send_async(self, content):
        room = self._get_room()
        return await room.send_danmaku(Danmaku(content))

    def _load_recent_sent(self):
        if not self._recent_sent_file.exists():
            return []

        try:
            return json.loads(self._recent_sent_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug(f"读取最近发送弹幕缓存失败: {e}")
            return []

    def _save_recent_sent(self):
        try:
            self._recent_sent_file.write_text(
                json.dumps(list(self._recent_sent), ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug(f"写入最近发送弹幕缓存失败: {e}")

    def is_own_message(self, username, content, uid=None, window_seconds=15):
        now = time.monotonic()
        cached_records = self._load_recent_sent()
        if cached_records:
            self._recent_sent = deque(cached_records, maxlen=20)

        while self._recent_sent and now - self._recent_sent[0]["timestamp"] > window_seconds:
            self._recent_sent.popleft()
        self._save_recent_sent()

        uid = str(uid) if uid not in [None, ""] else None
        username = (username or "").strip()
        content = (content or "").strip()
        normalized_content = self._normalize_content(content)

        for sent_record in self._recent_sent:
            sent_uid = sent_record["uid"]
            sent_username = sent_record["username"]
            sent_content = sent_record["content"]
            sent_timestamp = sent_record["timestamp"]
            sent_normalized_content = sent_record.get("normalized_content") or self._normalize_content(sent_content)

            if (
                uid is not None
                and sent_uid is not None
                and uid == sent_uid
                and sent_normalized_content == normalized_content
            ):
                logger.debug(
                    f"过滤自身发送的弹幕(uid_match): uid={uid}, username={username}, content={content}"
                )
                return True

            if (
                sent_username
                and username
                and sent_username == username
                and sent_normalized_content == normalized_content
            ):
                logger.debug(
                    f"过滤自身发送的弹幕(username_match): uid={uid}, username={username}, content={content}"
                )
                return True

            if sent_normalized_content == normalized_content and now - sent_timestamp <= 8:
                logger.debug(
                    f"过滤自身发送的弹幕(content_fallback): uid={uid}, username={username}, content={content}"
                )
                return True

        return False

    def send_reply(self, username, content):
        if not self.enabled():
            return False

        send_content = self.format_reply(username, content)
        if not send_content:
            return False

        cookie = self.config.get("bilibili", "cookie") or ""
        if not self._parse_cookie_data(cookie, "bili_jct"):
            logger.warning("B站弹幕发送失败: 当前 cookie 缺少 bili_jct，无法发送公屏弹幕")
            self._webui_callback("当前 cookie 缺少 bili_jct，无法发送公屏弹幕", success=False)
            return False

        try:
            result = asyncio.run(self._send_async(send_content))
            sender_name = (
                result.get("mode_info", {}).get("user", {}).get("base", {}).get("name")
            )
            sender_uid = (
                result.get("mode_info", {}).get("user", {}).get("base", {}).get("uid")
            )
            if sender_name:
                self._sender_username = sender_name
            if sender_uid:
                self._sender_uid = str(sender_uid)

            record = {
                "uid": self._sender_uid or (str(sender_uid) if sender_uid not in [None, ""] else None),
                "username": (self._sender_username or sender_name or username or "").strip(),
                "content": send_content,
                "normalized_content": self._normalize_content(send_content),
                "timestamp": time.monotonic(),
            }
            self._recent_sent.append(record)
            self._save_recent_sent()

            logger.info(f"B站弹幕发送成功: {send_content}")
            logger.debug(
                f"记录最近发送弹幕: uid={record['uid']}, username={record['username']}, content={record['content']}"
            )
            logger.debug(f"B站弹幕发送返回: {result}")
            self._webui_callback(send_content, success=True, username=username)
            return True
        except Exception as e:
            logger.warning(f"B站弹幕发送失败: {e}")
            self._webui_callback(str(e), success=False)
            return False
