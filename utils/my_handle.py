import asyncio
import json
import os
import random
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from .audio import Audio
from .bilibili_sender import BilibiliLiveSender
from .common import Common
from .config import Config
from .db import SQLiteDB
from .gpt_model.gpt import GPT_MODEL
from .my_log import logger


class SingletonMeta(type):
    _instances = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        with cls._lock:
            if cls not in cls._instances:
                cls._instances[cls] = super().__call__(*args, **kwargs)
            return cls._instances[cls]


class My_handle(metaclass=SingletonMeta):
    common = None
    config = None
    audio = None
    bilibili_sender = None

    is_handleing = 0
    abnormal_alarm_data = {
        "platform": {"error_count": 0},
        "llm": {"error_count": 0},
        "tts": {"error_count": 0},
        "svc": {"error_count": 0},
        "visual_body": {"error_count": 0},
        "other": {"error_count": 0},
    }
    live_data = {"comment": [], "gift": [], "entrance": [], "follow": []}
    task_data = {"read_comment": {"data": [], "time": 0}, "thanks": {"gift": {"data": [], "time": 0}, "entrance": {"data": [], "time": 0}, "follow": {"data": [], "time": 0}}}
    waiting_queue = []
    waiting_queue_lock = threading.Lock()
    chatter_mode_enabled = False
    chatter_mode_thread = None
    chatter_mode_stop_event = threading.Event()
    live_welcomed_users = set()
    live_welcomed_users_lock = threading.Lock()
    recent_comment_keys = {}
    recent_comment_keys_lock = threading.Lock()

    def __init__(self, config_path):
        logger.info("初始化My_handle...")
        if My_handle.common is None:
            My_handle.common = Common()
        if My_handle.config is None:
            My_handle.config = Config(config_path)
        if My_handle.audio is None:
            My_handle.audio = Audio(config_path)
        if My_handle.bilibili_sender is None:
            My_handle.bilibili_sender = BilibiliLiveSender(My_handle.config)

        self.config_path = config_path
        self.config = My_handle.config
        self.last_voice_mode = None
        self.data_lock = threading.Lock()
        self.timers = {}
        self.zhipu = None
        self.chat_type_list = ["zhipu"]
        self.db = None
        self.memory_lock = threading.Lock()
        self.memory_path = Path("data") / "lingya_memory.json"
        self.memory_store = {}

        self._init_db()
        self._load_memory_store()
        self.config_load()
        self.start_timers()

    def _init_db(self):
        db_path = My_handle.config.get("database", "path")
        self.db = SQLiteDB(db_path)
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS danmu (
                username TEXT NOT NULL,
                content TEXT NOT NULL,
                ts DATETIME NOT NULL
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS entrance (
                username TEXT NOT NULL,
                ts DATETIME NOT NULL
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS gift (
                username TEXT NOT NULL,
                gift_name TEXT NOT NULL,
                gift_num INT NOT NULL,
                unit_price REAL NOT NULL,
                total_price REAL NOT NULL,
                ts DATETIME NOT NULL
            )
            """
        )

    def config_load(self):
        self.config = My_handle.config
        self.handle_chat_type()
        My_handle.chatter_mode_enabled = bool(My_handle.config.get("chatter_mode", "default_on"))
        My_handle.chatter_mode_stop_event = threading.Event()
        if not My_handle.chatter_mode_enabled:
            My_handle.chatter_mode_stop_event.set()

    def reload_config(self, config_path):
        My_handle.config = Config(config_path)
        My_handle.audio.reload_config(config_path)
        self.config_load()

    def handle_chat_type(self):
        chat_type = My_handle.config.get("chat_type")
        if chat_type != "zhipu":
            logger.warning(f"已精简版本仅保留 zhipu，当前 chat_type={chat_type}")
            return
        GPT_MODEL.set_model_config("zhipu", My_handle.config.get("zhipu"))
        self.zhipu = GPT_MODEL.get("zhipu")

    def get_room_id(self):
        return My_handle.config.get("room_display_id")

    def clear_queue(self, type: str = "message_queue"):
        return My_handle.audio.clear_queue(type)

    def stop_audio(self, type: str = "pygame", mixer_normal: bool = True, mixer_copywriting: bool = True):
        return My_handle.audio.stop_audio(type, mixer_normal, mixer_copywriting)

    def is_audio_queue_empty(self):
        return My_handle.audio.is_audio_queue_empty()

    def is_queue_less_or_greater_than(self, type: str = "message_queue", less: int = None, greater: int = None):
        return My_handle.audio.is_queue_less_or_greater_than(type, less, greater)

    def get_audio_info(self):
        return My_handle.audio.get_audio_info()

    def audio_synthesis_handle(self, data_json):
        if "content" in data_json and data_json["content"]:
            data_json["content"] = data_json["content"].replace("\n", "")
        My_handle.audio.audio_synthesis(data_json)

    def _load_memory_store(self):
        try:
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            if self.memory_path.exists():
                self.memory_store = json.loads(self.memory_path.read_text(encoding="utf-8"))
            else:
                self.memory_store = {}
        except Exception:
            logger.error(traceback.format_exc())
            self.memory_store = {}

    def _save_memory_store(self):
        try:
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            self.memory_path.write_text(json.dumps(self.memory_store, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.error(traceback.format_exc())

    def _memory_config(self):
        return My_handle.config.get("memory") or {}

    def _update_user_last_seen(self, username: str):
        username = (username or "").strip()
        if not username:
            return
        with self.memory_lock:
            entry = self.memory_store.setdefault(username, {"last_seen": "", "notes": []})
            entry["last_seen"] = My_handle.common.get_bj_time(0)
            self._save_memory_store()

    def _remember_user_note(self, username: str, note: str):
        username = (username or "").strip()
        note = re.sub(r"\s+", " ", (note or "").strip())
        if not username or not note:
            return
        config = self._memory_config()
        note = note[: int(config.get("note_max_len", 24) or 24)].strip(" ，,。")
        if not note:
            return
        with self.memory_lock:
            entry = self.memory_store.setdefault(username, {"last_seen": "", "notes": []})
            notes = [item for item in entry.get("notes", []) if item != note]
            notes.insert(0, note)
            entry["notes"] = notes[: int(config.get("max_notes_per_user", 5) or 5)]
            entry["last_seen"] = My_handle.common.get_bj_time(0)
            self._save_memory_store()

    def _extract_memory_candidates(self, username: str, content: str, reply: str):
        if not self._memory_config().get("enable", True):
            return
        content = (content or "").strip()
        for pattern in [
            r"我叫(.{1,12})",
            r"我是(.{1,12})",
            r"我喜欢(.{1,16})",
            r"我最爱(.{1,16})",
            r"最近在玩(.{1,16})",
            r"单推(.{1,12})",
            r"推的是(.{1,12})",
        ]:
            match = re.search(pattern, content)
            if match:
                self._remember_user_note(username, match.group(0))
                return
        if re.search(r"(上班|下班|加班|考试|作业)", content):
            keyword = re.search(r"(上班|下班|加班|考试|作业)", content).group(1)
            self._remember_user_note(username, f"最近提过{keyword}")
        if re.search(r"(晚安|睡了|先走了|下播见)", content):
            self._remember_user_note(username, "常来直播间")

    def _build_memory_context(self, username: str):
        if not self._memory_config().get("enable", True):
            return ""
        username = (username or "").strip()
        if not username:
            return ""
        with self.memory_lock:
            entry = self.memory_store.get(username) or {}
            notes = entry.get("notes") or []
        if not notes:
            return ""
        notes = notes[: int(self._memory_config().get("max_prompt_notes", 3) or 3)]
        return f"\n你对观众“{username}”的已知小记忆：{'；'.join(notes)}\n回复时可以自然带一下，但不要机械复述。\n"

    def parse_voice_mode_and_clean(self, content: str):
        if content is None:
            return None, None
        match = re.match(r"^\s*\[\[(CUTE|REAL)\]\]\s*", content)
        if not match:
            return None, content
        voice_mode = match.group(1).lower()
        cleaned_content = re.sub(r"^\s*\[\[(CUTE|REAL)\]\]\s*", "", content, count=1)
        return voice_mode, cleaned_content

    def _reply_needs_style_rewrite(self, resp_content: str):
        if not resp_content:
            return False
        stripped = resp_content.strip()
        if not re.match(r"^\s*\[\[(CUTE|REAL)\]\]", stripped):
            return True
        text_without_marker = re.sub(r"^\s*\[\[(CUTE|REAL)\]\]\s*", "", stripped, count=1)
        sentences = [s for s in re.split(r"[。！？!?]", text_without_marker) if s.strip()]
        if len(sentences) > 2:
            return True
        if any(len(sentence.strip()) > 40 for sentence in sentences):
            return True
        return any(phrase in stripped for phrase in ["如果您有任何问题", "很高兴", "我会尽力", "为您提供"])

    def _pick_live_voice_mode(self, source_content: str, resp_content: str):
        merged = f"{source_content or ''} {resp_content or ''}"
        if re.search(r"\[\[\s*REAL\s*\]\]", resp_content or "", re.IGNORECASE):
            return "REAL"
        if re.search(r"(累了|困了|收声|先歇会|顶不住|麻了|无语|烦|绷不住|下班|歇会|好累|困死)", merged):
            return "REAL"
        if re.search(r"(深夜|半夜|凌晨|安静|没人|冷场|无聊)", source_content or "") and random.random() < 0.35:
            return "REAL"
        if re.search(r"(装累了|别装了|恢复原样|原声线|别夹了)", source_content or ""):
            return "REAL"
        return "CUTE"

    def _voice_mode_key(self, voice_mode: str):
        return "real" if str(voice_mode or "").upper() == "REAL" else "cute"

    def _get_live_mode_prompt(self, voice_mode: str):
        mode_key = self._voice_mode_key(voice_mode)
        persona_modes = My_handle.config.get("persona_modes") or {}
        mode_config = persona_modes.get(mode_key) or {}
        return mode_config.get("prompt") or My_handle.config.get("before_prompt") or ""

    def _build_live_prompt(self, voice_mode: str, content: str, username: str = ""):
        return self._get_live_mode_prompt(voice_mode) + self._build_memory_context(username) + (content or "") + (My_handle.config.get("after_prompt") or "")

    def _fallback_live_reply(self, source_content: str = "", resp_content: str = ""):
        source = (source_content or "").strip()
        voice_mode = self._pick_live_voice_mode(source_content, resp_content)
        if voice_mode == "REAL":
            if re.search(r"晚上好|晚安前", source):
                text = "晚上好。我还醒着"
            elif re.search(r"早上好|早安", source):
                text = "早。我在"
            elif re.search(r"中午好", source):
                text = "中午好。先去吃饭"
            elif re.search(r"在吗|在不在|有人吗", source):
                text = "在。没掉线"
            elif re.search(r"你是谁|你叫啥|你叫什么|名字", source):
                text = "我叫铃芽。别乱叫"
            elif re.search(r"装可爱|猫娘|女仆", source):
                text = "那是营业。现在先不装"
            else:
                text = "行，我在。你继续说"
        else:
            if re.search(r"晚上好|晚安前", source):
                text = "晚上好呀，铃芽在呢"
            elif re.search(r"早上好|早安", source):
                text = "早呀，铃芽来值班了"
            elif re.search(r"中午好", source):
                text = "中午好呀，先记得吃饭"
            elif re.search(r"在吗|在不在|有人吗", source):
                text = "在呢在呢，铃芽没挂机"
            elif re.search(r"你是谁|你叫啥|你叫什么|名字", source):
                text = "我叫铃芽，别喊女仆*啦"
            elif re.search(r"装可爱|猫娘|女仆", source):
                text = "那铃芽努力营业一下喵"
            else:
                text = "铃芽在呢，你接着说呀"
        return f"[[{voice_mode}]] {text[:40]}"

    def _rewrite_reply_to_live_style(self, source_content: str, resp_content: str):
        raw_text = (resp_content or "").strip()
        voice_mode = self._pick_live_voice_mode(source_content, raw_text)
        text = re.sub(r"\[\[\s*(CUTE|REAL)\s*\]\]\s*", "", raw_text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        if any(marker in text for marker in ["逐条解释", "请提供更多的上下文", "以下是逐条解析", "1.", "2.", "3."]):
            return self._fallback_live_reply(source_content, resp_content)
        sentences = []
        for sentence in re.split(r"[。！？!?]", text):
            cleaned = sentence.strip(" ，,。！？!?\"'")
            if not cleaned:
                continue
            limit = 28 if voice_mode == "REAL" else 40
            sentences.append(cleaned[:limit].rstrip(" ，,。！？!?"))
        if not sentences:
            return self._fallback_live_reply(source_content, resp_content)
        cleaned_text = "。".join(sentences[:2]).strip("。")
        rewritten = f"[[{voice_mode}]] {cleaned_text}"
        return rewritten if not self._reply_needs_style_rewrite(rewritten) else self._fallback_live_reply(source_content, resp_content)

    def llm_handle(self, chat_type, data, type="chat", webui_show=True):
        try:
            if type != "chat":
                return None
            if My_handle.config.get("filter", "before_must_str_for_llm"):
                if not any(data["ori_content"].startswith(prefix) for prefix in My_handle.config.get("filter", "before_must_str_for_llm")):
                    return None
            if My_handle.config.get("filter", "after_must_str_for_llm"):
                if not any(data["ori_content"].endswith(prefix) for prefix in My_handle.config.get("filter", "after_must_str_for_llm")):
                    return None
            if chat_type == "reread":
                resp_content = data["content"]
            else:
                if self.zhipu is None:
                    self.handle_chat_type()
                resp_content = self.zhipu.get_resp(data["content"])
            if resp_content is not None:
                resp_content = resp_content.strip()
                resp_content = re.sub(r"\\n|\n", "", resp_content)
                filter_state = {"is_filtering": False, "current_tag": None, "buffer": ""}
                resp_content = My_handle.common.llm_resp_content_filter_tags(resp_content, filter_state)
            if My_handle.config.get("reply_template", "enable") and resp_content is not None:
                variables = {
                    "username": data["username"][: self.config.get("reply_template", "username_max_len")],
                    "data": resp_content,
                    "cur_time": My_handle.common.get_bj_time(5),
                }
                template = My_handle.common.get_list_random_or_default(self.config.get("reply_template", "copywriting"), "{data}")
                if any(var in template for var in variables):
                    resp_content = template.format(**{var: value for var, value in variables.items() if var in template})
            if webui_show and resp_content:
                self.webui_show_chat_log_callback(chat_type, data, resp_content)
            return resp_content
        except Exception:
            logger.error(traceback.format_exc())
            return None

    def llm_stream_handle_and_audio_synthesis(self, chat_type, data, type="chat", webui_show=True):
        return self.llm_handle(chat_type, data, type=type, webui_show=webui_show)

    def webui_show_chat_log_callback(self, data_type: str, data: dict, resp_content: str):
        try:
            if not My_handle.config.get("talk", "show_chat_log"):
                return
            if "ori_username" not in data:
                data["ori_username"] = data["username"]
            if "ori_content" not in data:
                data["ori_content"] = data["content"]
            return_webui_json = {
                "type": "llm",
                "data": {
                    "type": data_type,
                    "username": data["ori_username"],
                    "content_type": "answer",
                    "content": f"错误：{data_type}无返回，请查看日志" if resp_content is None else resp_content,
                    "timestamp": My_handle.common.get_bj_time(0),
                },
            }
            webui_ip = "127.0.0.1" if My_handle.config.get("webui", "ip") == "0.0.0.0" else My_handle.config.get("webui", "ip")
            My_handle.common.send_request(f"http://{webui_ip}:{My_handle.config.get('webui', 'port')}/callback", "POST", return_webui_json, timeout=30)
        except Exception:
            logger.error(traceback.format_exc())

    def comment_check_and_replace(self, content):
        content = (content or "").strip()
        if My_handle.config.get("filter", "before_must_str") and not any(content.startswith(prefix) for prefix in My_handle.config.get("filter", "before_must_str")):
            return None
        for prefix in My_handle.config.get("filter", "before_must_str") or []:
            if content.startswith(prefix):
                content = content[len(prefix):]
                break
        if My_handle.config.get("filter", "after_must_str") and not any(content.endswith(prefix) for prefix in My_handle.config.get("filter", "after_must_str")):
            return None
        for suffix in My_handle.config.get("filter", "after_must_str") or []:
            if content.endswith(suffix):
                content = content[: -len(suffix)]
                break
        if My_handle.common.is_punctuation_string(content):
            return None
        content = content.replace("\n", ",")
        if My_handle.config.get("filter", "emoji"):
            content = re.sub(r"\[.*?\]", "", content)
        if My_handle.common.lang_check(content, My_handle.config.get("need_lang")) is None:
            return None
        return content

    def prohibitions_handle(self, content):
        if content is None:
            return None
        if My_handle.common.is_url_check(content):
            return None
        if My_handle.config.get("filter", "badwords", "enable"):
            if My_handle.common.profanity_content(content):
                return None
            bad_word = My_handle.common.check_sensitive_words2(My_handle.config.get("filter", "badwords", "path"), content)
            if bad_word is not None:
                if My_handle.config.get("filter", "badwords", "discard"):
                    return None
                content = content.replace(bad_word, My_handle.config.get("filter", "badwords", "replace"))
                return self.prohibitions_handle(content)
            if My_handle.config.get("filter", "badwords", "bad_pinyin_path"):
                if My_handle.common.check_sensitive_words3(My_handle.config.get("filter", "badwords", "bad_pinyin_path"), content):
                    return None
        return content

    def reread_handle(self, data, filter=False, type="reread"):
        try:
            content = data["content"]
            if filter:
                content = self.prohibitions_handle(content)
                if content is None:
                    return None
            message = {
                "type": type,
                "tts_type": My_handle.config.get("audio_synthesis_type"),
                "data": My_handle.config.get(My_handle.config.get("audio_synthesis_type")),
                "config": My_handle.config.get("filter"),
                "username": data.get("username", "铃芽"),
                "content": content,
            }
            self.audio_synthesis_handle(message)
            return message
        except Exception:
            logger.error(traceback.format_exc())
            return None

    def blacklist_handle(self, data):
        if not My_handle.config.get("filter", "blacklist", "enable"):
            return False
        usernames = My_handle.config.get("filter", "blacklist", "username") or []
        return data.get("username") in usernames

    def integral_handle(self, type, data):
        return False

    def local_qa_handle(self, data):
        return False

    def choose_song_handle(self, data):
        return False

    def sd_handle(self, data):
        return False

    def key_mapping_handle(self, type, data):
        return False

    def custom_cmd_handle(self, type, data):
        return False

    def search_online_handle(self, content: str):
        return content

    def write_to_comment_log(self, content, data):
        try:
            comment_file_path = "./log/comment-" + My_handle.common.get_bj_time(1) + ".txt"
            with open(comment_file_path, "a", encoding="utf-8") as f:
                f.write(f"[{data['username']}] {data['content']}\n[铃芽] {content}\n")
        except Exception:
            logger.error(traceback.format_exc())

    def is_data_repeat_in_limited_time(self, data_type: str, data: dict):
        if not My_handle.config.get("filter", "limited_time_deduplication", "enable"):
            return False
        duration = float(My_handle.config.get("filter", "limited_time_deduplication", data_type) or 0)
        if duration <= 0:
            return False
        now = time.time()
        content = (data.get("content") or "").strip()
        username = (data.get("username") or "").strip()
        key = f"{username}:{content}"
        records = My_handle.live_data.setdefault(data_type, [])
        records[:] = [item for item in records if now - item["time"] <= duration]
        if any(item["key"] == key for item in records):
            return True
        records.append({"key": key, "time": now})
        return False

    def _normalize_queue_command(self, content: str):
        return re.sub(r"\s+", " ", (content or "").strip())

    def _queue_commands(self, key: str):
        commands = My_handle.config.get("queue", key) or []
        return [str(command).strip() for command in commands if str(command).strip()]

    def _queue_find_index(self, username: str):
        for index, item in enumerate(My_handle.waiting_queue):
            if item["username"] == username:
                return index
        return -1

    def _queue_send_reply(self, queue_data: dict, reply: str):
        if not reply:
            return None
        reply = reply[:40]
        reply = f"[[REAL]] {reply}"
        voice_mode, clean_content = self.parse_voice_mode_and_clean(reply)
        message = {
            "type": "comment",
            "tts_type": My_handle.config.get("audio_synthesis_type"),
            "data": My_handle.config.get(My_handle.config.get("audio_synthesis_type")),
            "config": My_handle.config.get("filter"),
            "username": queue_data["username"],
            "content": clean_content,
            "voice_mode": voice_mode,
        }
        if queue_data.get("platform") in ["bilibili", "bilibili2"] and My_handle.bilibili_sender is not None:
            My_handle.bilibili_sender.send_reply(queue_data["username"], clean_content)
        self.audio_synthesis_handle(message)
        return message

    def queue_handle(self, data):
        if not My_handle.config.get("queue", "enable"):
            return False
        username = data["username"]
        content = self._normalize_queue_command(data["content"])
        if content in self._queue_commands("my_status_cmd"):
            with My_handle.waiting_queue_lock:
                index = self._queue_find_index(username)
            reply = "你还没在队列里。" if index == -1 else f"你现在排第{index + 1}位。"
            self._queue_send_reply(data, reply)
            return True
        if content in self._queue_commands("list_cmd"):
            with My_handle.waiting_queue_lock:
                names = [item["username"] for item in My_handle.waiting_queue[: int(My_handle.config.get("queue", "show_limit") or 3)]]
                total = len(My_handle.waiting_queue)
            reply = "队列为空。" if total == 0 else f"当前前{len(names)}位：{'、'.join(names)}"
            self._queue_send_reply(data, reply)
            return True
        if content in self._queue_commands("leave_cmd"):
            with My_handle.waiting_queue_lock:
                index = self._queue_find_index(username)
                if index != -1:
                    My_handle.waiting_queue.pop(index)
            self._queue_send_reply(data, "已帮你取消排队。" if index != -1 else "你本来就不在队列里。")
            return True
        if content in self._queue_commands("next_cmd"):
            allow = My_handle.config.get("queue", "allow_all_users_manage") or username in (My_handle.config.get("queue", "admin_usernames") or [])
            if not allow:
                return False
            with My_handle.waiting_queue_lock:
                if My_handle.waiting_queue:
                    current = My_handle.waiting_queue.pop(0)
                    reply = f"下一位是{current['username']}。"
                else:
                    reply = "队列已经空了。"
            self._queue_send_reply(data, reply)
            return True
        if content in self._queue_commands("clear_cmd"):
            allow = My_handle.config.get("queue", "allow_all_users_manage") or username in (My_handle.config.get("queue", "admin_usernames") or [])
            if not allow:
                return False
            with My_handle.waiting_queue_lock:
                My_handle.waiting_queue.clear()
            self._queue_send_reply(data, "队列清空了。")
            return True
        for command in self._queue_commands("join_cmd"):
            if content == command or content.startswith(command + " "):
                note = content[len(command):].strip()[: int(My_handle.config.get("queue", "note_max_len") or 12)]
                with My_handle.waiting_queue_lock:
                    index = self._queue_find_index(username)
                    if index != -1:
                        if note:
                            My_handle.waiting_queue[index]["note"] = note
                        reply = f"你已经在队列里了，现在第{index + 1}位。"
                    elif len(My_handle.waiting_queue) >= int(My_handle.config.get("queue", "max_size") or 50):
                        reply = "排队人数满了，稍后再试吧。"
                    else:
                        My_handle.waiting_queue.append({"username": username, "note": note, "timestamp": datetime.now().isoformat()})
                        reply = f"已加入排队，现在第{len(My_handle.waiting_queue)}位。"
                self._queue_send_reply(data, reply)
                return True
        return False

    def _chatter_mode_trigger_usernames(self):
        usernames = My_handle.config.get("chatter_mode", "trigger_usernames") or []
        if not usernames:
            usernames = My_handle.config.get("queue", "admin_usernames") or []
        return [str(username).strip() for username in usernames if str(username).strip()]

    def _is_chatter_mode_trigger_user(self, username: str):
        return (username or "").strip() in self._chatter_mode_trigger_usernames()

    def _send_chatter_mode_reply(self, data: dict, resp_content: str):
        if not resp_content:
            return None
        voice_mode, clean_content = self.parse_voice_mode_and_clean(resp_content.strip().replace("\n", "。"))
        self.write_to_comment_log(clean_content, {"username": data["username"], "content": data["content"]})
        message = {
            "type": "comment",
            "tts_type": My_handle.config.get("audio_synthesis_type"),
            "data": My_handle.config.get(My_handle.config.get("audio_synthesis_type")),
            "config": My_handle.config.get("filter"),
            "username": data["username"],
            "content": clean_content,
            "voice_mode": voice_mode,
        }
        if data.get("platform") in ["bilibili", "bilibili2"] and My_handle.bilibili_sender is not None:
            My_handle.bilibili_sender.send_reply(data["username"], clean_content)
        self.audio_synthesis_handle(message)
        return message

    def _build_chatter_mode_prompt(self):
        voice_mode = "REAL" if random.random() < 0.2 else "CUTE"
        return self._build_live_prompt(voice_mode, "直播间有点安静，请主动找个轻松话题说一句。", My_handle.config.get("talk", "username") or "铃芽")

    def _generate_chatter_mode_content(self):
        mode_config = My_handle.config.get("chatter_mode") or {}
        fallback_copy = mode_config.get("fallback_copy") or []
        try:
            resp_content = self.llm_handle("zhipu", {
                "username": My_handle.config.get("talk", "username") or "铃芽",
                "content": self._build_chatter_mode_prompt(),
                "ori_username": My_handle.config.get("talk", "username") or "铃芽",
                "ori_content": "直播间有点安静",
            })
            if resp_content:
                return resp_content
        except Exception:
            logger.error(traceback.format_exc())
        return random.choice(fallback_copy) if fallback_copy else "[[CUTE]] 铃芽来陪大家聊两句呀"

    def _chatter_mode_loop(self, platform: str, username: str):
        while not My_handle.chatter_mode_stop_event.is_set():
            try:
                interval_min = int(My_handle.config.get("chatter_mode", "interval_min") or 6)
                interval_max = int(My_handle.config.get("chatter_mode", "interval_max") or interval_min)
                wait_seconds = max(1, random.randint(min(interval_min, interval_max), max(interval_min, interval_max)))
                if My_handle.chatter_mode_stop_event.wait(wait_seconds):
                    break
                self._send_chatter_mode_reply({"platform": platform, "username": username, "content": "直播间有点安静"}, self._generate_chatter_mode_content())
            except Exception:
                logger.error(traceback.format_exc())
                if My_handle.chatter_mode_stop_event.wait(3):
                    break

    def _set_chatter_mode(self, enabled: bool, data: dict):
        if not My_handle.config.get("chatter_mode", "enable"):
            return False
        if enabled:
            if My_handle.chatter_mode_enabled:
                return True
            My_handle.chatter_mode_enabled = True
            My_handle.chatter_mode_stop_event.clear()
            My_handle.chatter_mode_thread = threading.Thread(target=self._chatter_mode_loop, args=(data.get("platform"), data.get("username")), daemon=True)
            My_handle.chatter_mode_thread.start()
            self._queue_send_reply(data, My_handle.config.get("chatter_mode", "start_reply") or "铃芽进入喋喋不休模式。")
            return True
        My_handle.chatter_mode_enabled = False
        My_handle.chatter_mode_stop_event.set()
        self._queue_send_reply(data, My_handle.config.get("chatter_mode", "stop_reply") or "铃芽先收声。")
        return True

    def chatter_mode_handle(self, data: dict):
        if not self._is_chatter_mode_trigger_user(data.get("username")):
            return False
        content = (data.get("content") or "").strip()
        if content == (My_handle.config.get("chatter_mode", "start_cmd") or "").strip():
            return self._set_chatter_mode(True, data)
        if content == (My_handle.config.get("chatter_mode", "stop_cmd") or "").strip():
            return self._set_chatter_mode(False, data)
        return False

    def _make_entrance_key(self, data: dict):
        uid = data.get("uid")
        return f"uid:{uid}" if uid not in [None, ""] else f"name:{data.get('username')}"

    def _build_direct_message(self, message_type: str, username: str, resp_content: str, extra_data: dict = None):
        voice_mode, clean_content = self.parse_voice_mode_and_clean(resp_content)
        clean_content = clean_content or resp_content
        message = {
            "type": message_type,
            "tts_type": My_handle.config.get("audio_synthesis_type"),
            "data": My_handle.config.get(My_handle.config.get("audio_synthesis_type")),
            "config": My_handle.config.get("filter"),
            "username": username,
            "content": clean_content,
            "voice_mode": voice_mode,
        }
        if extra_data:
            message.update(extra_data)
        return message

    def _pick_special_entrance_copy(self, data: dict):
        special_config = My_handle.config.get("thanks", "special_entrance") or {}
        guard_level = int(data.get("guard_level", 0) or 0)
        medal_level = int(data.get("medal_level", 0) or data.get("fans_medal_level", 0) or 0)
        if guard_level > 0:
            copies = special_config.get("guard_copy") or []
            if copies:
                return random.choice(copies), {"guard_level": guard_level}
        if medal_level >= int(special_config.get("high_medal_min_level", 12) or 12):
            copies = special_config.get("high_medal_copy") or []
            if copies:
                return random.choice(copies), {"medal_level": medal_level}
        if medal_level > 0:
            copies = special_config.get("familiar_copy") or []
            if copies:
                return random.choice(copies), {"medal_level": medal_level}
        return None

    def _build_special_entrance_message(self, data: dict):
        special_config = My_handle.config.get("thanks", "special_entrance") or {}
        if not special_config.get("enable"):
            return None
        welcome_key = self._make_entrance_key(data)
        with My_handle.live_welcomed_users_lock:
            if special_config.get("only_first_time_per_live") and welcome_key in My_handle.live_welcomed_users:
                return None
        picked = self._pick_special_entrance_copy(data)
        if picked is None:
            return None
        resp_content, extra_data = picked
        resp_content = My_handle.common.brackets_text_randomize(resp_content)
        resp_content = My_handle.common.dynamic_variable_replacement(resp_content, {
            "username": data["username"],
            "medal_level": int(data.get("medal_level", 0) or data.get("fans_medal_level", 0) or 0),
            "guard_level": int(data.get("guard_level", 0) or 0),
        })
        with My_handle.live_welcomed_users_lock:
            My_handle.live_welcomed_users.add(welcome_key)
        return self._build_direct_message("entrance", data["username"], resp_content, extra_data)

    def comment_handle(self, data):
        try:
            username = data["username"]
            content = data["content"]
            if My_handle.bilibili_sender is not None and My_handle.bilibili_sender.is_own_message(username, content, data.get("uid")):
                return None
            self._update_user_last_seen(username)
            if self.is_data_repeat_in_limited_time("comment", data):
                return None
            if self.blacklist_handle(data):
                return None

            if My_handle.config.get("talk", "show_chat_log"):
                return_webui_json = {
                    "type": "llm",
                    "data": {
                        "type": "弹幕信息",
                        "username": data.get("ori_username") or username,
                        "user_face": data.get("user_face") or "https://robohash.org/ui",
                        "content_type": "question",
                        "content": data.get("ori_content") or content,
                        "timestamp": My_handle.common.get_bj_time(0),
                    },
                }
                webui_ip = "127.0.0.1" if My_handle.config.get("webui", "ip") == "0.0.0.0" else My_handle.config.get("webui", "ip")
                My_handle.common.send_request(f"http://{webui_ip}:{My_handle.config.get('webui', 'port')}/callback", "POST", return_webui_json, timeout=10)

            if My_handle.config.get("database", "comment_enable"):
                self.db.execute("INSERT INTO danmu (username, content, ts) VALUES (?, ?, ?)", (username, content, datetime.now()))

            username = My_handle.common.merge_consecutive_asterisks(username)
            username = self.prohibitions_handle(username)
            content = self.prohibitions_handle(content)
            if username is None or content is None:
                return None
            content = self.comment_check_and_replace(content)
            if content is None or My_handle.common.is_punctuation_string(content):
                return None

            if self.queue_handle({"platform": data.get("platform"), "username": username, "content": content}):
                return None
            if self.chatter_mode_handle({"platform": data.get("platform"), "username": username, "content": content, "uid": data.get("uid")}):
                return None

            if My_handle.config.get("read_comment", "enable"):
                try:
                    read_message = {
                        "type": "read_comment",
                        "tts_type": My_handle.config.get("audio_synthesis_type"),
                        "data": My_handle.config.get(My_handle.config.get("audio_synthesis_type")),
                        "config": My_handle.config.get("filter"),
                        "username": username,
                        "content": content,
                    }
                    if My_handle.config.get("read_comment", "read_username_enable"):
                        read_message["username"] = My_handle.common.replace_special_characters(read_message["username"], "！!@#￥$%^&*_-+/——=()（）【】}|{:;<>~`\\")
                        read_message["username"] = read_message["username"][: self.config.get("read_comment", "username_max_len")]
                        if My_handle.config.get("filter", "username_convert_digits_to_chinese"):
                            read_message["username"] = My_handle.common.convert_digits_to_chinese(read_message["username"])
                        templates = self.config.get("read_comment", "read_username_copywriting") or []
                        if templates:
                            template = random.choice(templates)
                            if "{username}" in template:
                                read_message["content"] = template.format(username=read_message["username"]) + read_message["content"]
                    if My_handle.config.get("read_comment", "periodic_trigger", "enable"):
                        My_handle.task_data["read_comment"]["data"].append(read_message)
                    else:
                        self.audio_synthesis_handle(read_message)
                except Exception:
                    logger.error(traceback.format_exc())

            selected_voice_mode = self._pick_live_voice_mode(content, "")
            llm_content = content
            if self.config.get("comment_template", "enable"):
                variables = {"username": username, "comment": content, "cur_time": My_handle.common.get_bj_time(5)}
                template = self.config.get("comment_template", "copywriting")
                if any(var in template for var in variables):
                    llm_content = template.format(**{var: value for var, value in variables.items() if var in template})
            data_json = {
                "username": username,
                "content": self._build_live_prompt(selected_voice_mode, llm_content, username),
                "ori_username": data.get("ori_username") or username,
                "ori_content": data.get("ori_content") or content,
            }
            chat_type = My_handle.config.get("chat_type")
            resp_content = self.llm_stream_handle_and_audio_synthesis(chat_type, data_json) if self.config.get(chat_type, "stream") else self.llm_handle(chat_type, data_json)
            if not resp_content:
                return None
            if not re.match(r"^\s*\[\[(CUTE|REAL)\]\]", resp_content):
                resp_content = f"[[{selected_voice_mode}]] {resp_content.strip()}"
            if self._reply_needs_style_rewrite(resp_content):
                resp_content = self._rewrite_reply_to_live_style(content, resp_content)
            voice_mode, clean_content = self.parse_voice_mode_and_clean(resp_content)
            clean_content = self.prohibitions_handle((clean_content or "").replace("\n", "。").strip())
            if not clean_content:
                return None
            self.last_voice_mode = voice_mode
            self._extract_memory_candidates(username, content, clean_content)
            self.write_to_comment_log(clean_content, {"username": username, "content": content})
            message = {
                "type": "comment",
                "tts_type": My_handle.config.get("audio_synthesis_type"),
                "data": My_handle.config.get(My_handle.config.get("audio_synthesis_type")),
                "config": My_handle.config.get("filter"),
                "username": username,
                "content": clean_content,
                "voice_mode": voice_mode,
            }
            if data.get("platform") in ["bilibili", "bilibili2"] and My_handle.bilibili_sender is not None:
                My_handle.bilibili_sender.send_reply(username, clean_content)
            self.audio_synthesis_handle(message)
            return message
        except Exception:
            logger.error(traceback.format_exc())
            return None

    def gift_handle(self, data):
        try:
            if self.is_data_repeat_in_limited_time("gift", data):
                return None
            if My_handle.config.get("database", "gift_enable"):
                self.db.execute(
                    "INSERT INTO gift (username, gift_name, gift_num, unit_price, total_price, ts) VALUES (?, ?, ?, ?, ?, ?)",
                    (data["username"], data["gift_name"], data["num"], data["unit_price"], data["total_price"], datetime.now()),
                )
            if not My_handle.config.get("thanks", "gift_enable"):
                return None
            username = My_handle.common.merge_consecutive_asterisks(data["username"])
            username = My_handle.common.replace_special_characters(username, "！!@#￥$%^&*_-+/——=()（）【】}|{:;<>~`\\")
            username = username[: self.config.get("thanks", "username_max_len")]
            if My_handle.config.get("filter", "username_convert_digits_to_chinese"):
                username = My_handle.common.convert_digits_to_chinese(username)
            template = random.choice(self.config.get("thanks", "gift_copy") or ["谢谢{username}的礼物"])
            content = My_handle.common.dynamic_variable_replacement(template, {
                "username": username,
                "gift_name": data["gift_name"],
                "gift_num": data["num"],
                "total_price": data["total_price"],
            })
            message = self._build_direct_message("gift", username, content)
            self.audio_synthesis_handle(message)
            return message
        except Exception:
            logger.error(traceback.format_exc())
            return None

    def entrance_handle(self, data):
        try:
            if self.is_data_repeat_in_limited_time("entrance", data):
                return None
            if My_handle.config.get("database", "entrance_enable"):
                self.db.execute("INSERT INTO entrance (username, ts) VALUES (?, ?)", (data["username"], datetime.now()))
            special_message = self._build_special_entrance_message(data)
            if special_message is not None:
                self.audio_synthesis_handle(special_message)
                return special_message
            if not My_handle.config.get("thanks", "entrance_enable"):
                return None
            username = My_handle.common.merge_consecutive_asterisks(data["username"])
            username = username[: self.config.get("thanks", "username_max_len")]
            if My_handle.config.get("filter", "username_convert_digits_to_chinese"):
                username = My_handle.common.convert_digits_to_chinese(username)
            content = random.choice(self.config.get("thanks", "entrance_copy") or ["欢迎{username}"])
            content = My_handle.common.dynamic_variable_replacement(content, {"username": username})
            message = self._build_direct_message("entrance", username, content)
            self.audio_synthesis_handle(message)
            return message
        except Exception:
            logger.error(traceback.format_exc())
            return None

    def follow_handle(self, data):
        try:
            if self.is_data_repeat_in_limited_time("follow", data):
                return None
            if not My_handle.config.get("thanks", "follow_enable"):
                return None
            username = My_handle.common.merge_consecutive_asterisks(data["username"])
            username = username[: self.config.get("thanks", "username_max_len")]
            content = random.choice(self.config.get("thanks", "follow_copy") or ["谢谢关注，{username}"])
            content = My_handle.common.dynamic_variable_replacement(content, {"username": username})
            message = self._build_direct_message("follow", username, content)
            self.audio_synthesis_handle(message)
            return message
        except Exception:
            logger.error(traceback.format_exc())
            return None

    def schedule_handle(self, data):
        try:
            message = {
                "type": "schedule",
                "tts_type": My_handle.config.get("audio_synthesis_type"),
                "data": My_handle.config.get(My_handle.config.get("audio_synthesis_type")),
                "config": My_handle.config.get("filter"),
                "username": data["username"],
                "content": data["content"],
            }
            self.audio_synthesis_handle(message)
            return message
        except Exception:
            logger.error(traceback.format_exc())
            return None

    def idle_time_task_handle(self, data):
        try:
            if data.get("type") == "local_audio":
                message = {
                    "type": "idle_time_task",
                    "tts_type": My_handle.config.get("audio_synthesis_type"),
                    "data": My_handle.config.get(My_handle.config.get("audio_synthesis_type")),
                    "config": My_handle.config.get("filter"),
                    "username": data["username"],
                    "content": data["content"],
                    "content_type": data.get("type"),
                    "file_path": os.path.abspath(data["file_path"]),
                }
                self.audio_synthesis_handle(message)
                return message
            selected_voice_mode = self._pick_live_voice_mode(data["content"], "")
            prompt = self._build_live_prompt(selected_voice_mode, data["content"], data["username"])
            resp_content = self.llm_handle(My_handle.config.get("chat_type"), {
                "username": data["username"],
                "content": prompt,
                "ori_username": data["username"],
                "ori_content": data["content"],
            })
            if not resp_content:
                return None
            if not re.match(r"^\s*\[\[(CUTE|REAL)\]\]", resp_content):
                resp_content = f"[[{selected_voice_mode}]] {resp_content.strip()}"
            if self._reply_needs_style_rewrite(resp_content):
                resp_content = self._rewrite_reply_to_live_style(data["content"], resp_content)
            voice_mode, clean_content = self.parse_voice_mode_and_clean(resp_content)
            clean_content = self.prohibitions_handle((clean_content or "").replace("\n", "。").strip())
            if not clean_content:
                return None
            self._extract_memory_candidates(data["username"], data["content"], clean_content)
            self.write_to_comment_log(clean_content, {"username": data["username"], "content": data["content"]})
            message = {
                "type": "idle_time_task",
                "tts_type": My_handle.config.get("audio_synthesis_type"),
                "data": My_handle.config.get(My_handle.config.get("audio_synthesis_type")),
                "config": My_handle.config.get("filter"),
                "username": data["username"],
                "content": clean_content,
                "voice_mode": voice_mode,
                "content_type": data.get("type"),
            }
            if data.get("platform") in ["bilibili", "bilibili2"] and My_handle.bilibili_sender is not None:
                My_handle.bilibili_sender.send_reply(data["username"], clean_content)
            self.audio_synthesis_handle(message)
            return message
        except Exception:
            logger.error(traceback.format_exc())
            return None

    def image_recognition_schedule_handle(self, data):
        logger.warning("已精简版本不再支持图像识别定时任务")
        return None

    def talk_handle(self, data):
        logger.warning("已精简版本不再支持 talk 模式")
        return None

    def process_data(self, data, timer_flag):
        with self.data_lock:
            if timer_flag not in self.timers or not self.timers[timer_flag].is_alive():
                self.timers[timer_flag] = threading.Timer(self.get_interval(timer_flag), self.process_last_data, args=(timer_flag,))
                self.timers[timer_flag].start()
            if hasattr(self.timers[timer_flag], "last_data"):
                self.timers[timer_flag].last_data.append(data)
                reserve_num = int(My_handle.config.get("filter", f"{timer_flag}_forget_reserve_num") or 1)
                if len(self.timers[timer_flag].last_data) > reserve_num:
                    self.timers[timer_flag].last_data.pop(0)
            else:
                self.timers[timer_flag].last_data = [data]

    def process_last_data(self, timer_flag):
        with self.data_lock:
            timer = self.timers.get(timer_flag)
            if not timer or not getattr(timer, "last_data", None):
                return
            My_handle.is_handleing = 1
            handlers = {
                "comment": self.comment_handle,
                "gift": self.gift_handle,
                "entrance": self.entrance_handle,
                "follow": self.follow_handle,
                "talk": self.talk_handle,
                "schedule": self.schedule_handle,
                "idle_time_task": self.idle_time_task_handle,
                "image_recognition_schedule": self.image_recognition_schedule_handle,
            }
            handler = handlers.get(timer_flag)
            if handler is not None:
                for item in timer.last_data:
                    handler(item)
            My_handle.is_handleing = 0
            timer.last_data = []

    def get_interval(self, timer_flag):
        intervals = {
            "comment": My_handle.config.get("filter", "comment_forget_duration"),
            "gift": My_handle.config.get("filter", "gift_forget_duration"),
            "entrance": My_handle.config.get("filter", "entrance_forget_duration"),
            "follow": My_handle.config.get("filter", "follow_forget_duration"),
            "talk": My_handle.config.get("filter", "talk_forget_duration"),
            "schedule": My_handle.config.get("filter", "schedule_forget_duration"),
            "idle_time_task": My_handle.config.get("filter", "idle_time_task_forget_duration"),
        }
        return intervals.get(timer_flag, 0.1)

    def start_timers(self):
        pass

    def abnormal_alarm_handle(self, type):
        try:
            My_handle.abnormal_alarm_data[type]["error_count"] += 1
        except Exception:
            logger.error(traceback.format_exc())
        return True
