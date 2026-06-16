import asyncio
import os
import threading
import time
import traceback
from pathlib import Path

from pydub import AudioSegment

from .common import Common
from .config import Config
from .my_log import logger
from utils.audio_handle.audio_player import AUDIO_PLAYER
from utils.audio_handle.my_tts import MY_TTS


class Audio:
    mixer_normal = None
    mixer_copywriting = None
    audio_player = None

    message_queue = []
    message_queue_lock = threading.Lock()
    message_queue_not_empty = threading.Condition(lock=message_queue_lock)

    voice_tmp_path_queue = []
    voice_tmp_path_queue_lock = threading.Lock()
    voice_tmp_path_queue_not_empty = threading.Condition(lock=voice_tmp_path_queue_lock)

    abnormal_alarm_data = {
        "platform": {"error_count": 0},
        "llm": {"error_count": 0},
        "tts": {"error_count": 0},
        "svc": {"error_count": 0},
        "visual_body": {"error_count": 0},
        "other": {"error_count": 0},
    }

    copywriting_play_flag = 0
    unpause_copywriting_play_timer = None

    def __init__(self, config_path, type=1):
        self.config_path = config_path
        self.config = Config(config_path)
        self.common = Common()
        self.my_tts = MY_TTS(config_path)
        self.only_play_copywriting_thread = None

        if type == 2:
            logger.info("文案模式的Audio初始化...")
            return

        if self.config.get("play_audio", "player") == "pygame":
            import pygame

            Audio.mixer_normal = pygame.mixer
            Audio.mixer_copywriting = pygame.mixer

        Audio.audio_player = AUDIO_PLAYER(self.config.get("audio_player"))

        threading.Thread(target=lambda: asyncio.run(self.message_queue_thread()), daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(self.only_play_audio()), daemon=True).start()

    def reload_config(self, config_path):
        self.config_path = config_path
        self.config = Config(config_path)
        self.my_tts = MY_TTS(config_path)

    def clear_queue(self, type: str = "message_queue"):
        try:
            if type == "voice_tmp_path_queue":
                with Audio.voice_tmp_path_queue_lock:
                    Audio.voice_tmp_path_queue.clear()
                return True
            with Audio.message_queue_lock:
                Audio.message_queue.clear()
            return True
        except Exception:
            logger.error(traceback.format_exc())
            return False

    def stop_audio(self, type: str = "pygame", mixer_normal: bool = True, mixer_copywriting: bool = True):
        try:
            if type != "pygame":
                return False
            if mixer_normal and Audio.mixer_normal is not None:
                Audio.mixer_normal.music.stop()
            if mixer_copywriting and Audio.mixer_copywriting is not None:
                Audio.mixer_copywriting.music.stop()
            return True
        except Exception:
            logger.error(traceback.format_exc())
            return False

    def is_queue_less_or_greater_than(self, type: str = "message_queue", less: int = None, greater: int = None):
        queue = Audio.voice_tmp_path_queue if type == "voice_tmp_path_queue" else Audio.message_queue
        if less is not None:
            return len(queue) < less
        if greater is not None:
            return len(queue) > greater
        return False

    def get_audio_info(self):
        return {
            "wait_play_audio_num": len(Audio.voice_tmp_path_queue),
            "wait_synthesis_msg_num": len(Audio.message_queue),
        }

    def is_audio_queue_empty(self):
        flag = 0
        if len(Audio.message_queue) == 0:
            flag += 1
        if len(Audio.voice_tmp_path_queue) == 0:
            flag += 2
        if self.config.get("play_audio", "player") == "pygame" and Audio.mixer_normal is not None:
            if not Audio.mixer_normal.music.get_busy():
                flag += 4
        return flag

    def _get_priority(self, data_json: dict) -> int:
        priority_mapping = self.config.get("filter", "priority_mapping") or {}
        return int(priority_mapping.get(data_json.get("type"), 999))

    def _insert_queue_item(self, queue_name: str, data_json: dict):
        queue = Audio.voice_tmp_path_queue if queue_name == "voice" else Audio.message_queue
        lock = Audio.voice_tmp_path_queue_lock if queue_name == "voice" else Audio.message_queue_lock
        condition = Audio.voice_tmp_path_queue_not_empty if queue_name == "voice" else Audio.message_queue_not_empty
        max_len_key = "voice_tmp_path_queue_max_len" if queue_name == "voice" else "message_queue_max_len"
        max_len = int(self.config.get("filter", max_len_key) or 100)
        new_priority = self._get_priority(data_json)

        with lock:
            insert_position = len(queue)
            for index, item in enumerate(queue):
                if self._get_priority(item) > new_priority:
                    insert_position = index
                    break
            if insert_position >= max_len:
                logger.warning(f"{queue_name} queue 已满，丢弃：{data_json.get('content') or data_json.get('voice_path')}")
                return False
            queue.insert(insert_position, data_json)
            condition.notify()
        return True

    async def tts_handle(self, message):
        try:
            tts_type = message.get("tts_type")
            if tts_type == "none":
                return {
                    "result": {"code": 200, "message": "skip"},
                    "voice_path": message.get("file_path"),
                }

            if tts_type != "edge-tts":
                logger.warning(f"精简版仅保留 edge-tts，收到 {tts_type}")
                return {
                    "result": {"code": -1, "message": f"unsupported tts_type: {tts_type}"},
                    "voice_path": None,
                }

            edge_tts_config = dict(message.get("data") or {})
            voice_mode = message.get("voice_mode")
            if voice_mode:
                edge_tts_config.update((edge_tts_config.get("modes") or {}).get(voice_mode, {}))

            voice_path = await self.my_tts.edge_tts_api(
                {
                    "content": message.get("content", ""),
                    "edge-tts": edge_tts_config,
                }
            )
            if voice_path is None:
                self.abnormal_alarm_handle("tts")
                return {
                    "result": {"code": -1, "message": "tts failed"},
                    "voice_path": None,
                }
            return {
                "result": {"code": 200, "message": "success", "audio_path": voice_path},
                "voice_path": voice_path,
            }
        except Exception:
            logger.error(traceback.format_exc())
            self.abnormal_alarm_handle("tts")
            return {
                "result": {"code": -1, "message": "exception"},
                "voice_path": None,
            }

    async def send_audio_play_info_to_callback(self, data: dict = None):
        try:
            if not self.config.get("play_audio", "info_to_callback"):
                return None
            if data is None:
                data = {
                    "type": "audio_playback_completed",
                    "data": self.get_audio_info(),
                }
            main_api_ip = "127.0.0.1" if self.config.get("api_ip") == "0.0.0.0" else self.config.get("api_ip")
            return await self.common.send_async_request(
                f"http://{main_api_ip}:{self.config.get('api_port')}/callback",
                "POST",
                data,
            )
        except Exception:
            logger.error(traceback.format_exc())
            return None

    def audio_synthesis(self, message):
        try:
            if self.config.get("audio_synthesis_type") == "none" and message.get("tts_type") != "none":
                return

            if self.config.get("filter", "username_convert_digits_to_chinese") and message.get("username") is not None:
                message["username"] = self.common.convert_digits_to_chinese(message["username"])

            if not self._insert_queue_item("message", dict(message)):
                return
        except Exception:
            logger.error(traceback.format_exc())

    async def message_queue_thread(self):
        logger.info("创建音频合成消息队列线程")
        while True:
            try:
                with Audio.message_queue_lock:
                    while not Audio.message_queue:
                        Audio.message_queue_not_empty.wait()
                    message = Audio.message_queue.pop(0)
                await self.my_play_voice(message)
            except Exception:
                logger.error(traceback.format_exc())

    async def my_play_voice(self, message):
        try:
            if message.get("tts_type") == "none":
                voice_path = message.get("file_path") or message.get("voice_path")
                if not voice_path:
                    return False
                data_json = {
                    "type": message.get("type", "comment"),
                    "voice_path": os.path.abspath(voice_path),
                    "content": message.get("content", ""),
                }
                return self._insert_queue_item("voice", data_json)

            resp_json = await self.tts_handle(message)
            voice_path = resp_json.get("voice_path")
            if not voice_path:
                return False

            data_json = {
                "type": message.get("type", "comment"),
                "voice_path": os.path.abspath(voice_path),
                "content": message.get("content", ""),
            }
            return self._insert_queue_item("voice", data_json)
        except Exception:
            logger.error(traceback.format_exc())
            return False

    def audio_speed_change(self, audio_path, speed_factor=1.0, pitch_factor=1.0):
        audio = AudioSegment.from_file(audio_path)
        if speed_factor > 1.0:
            audio_changed = audio.speedup(playback_speed=speed_factor)
        elif speed_factor < 1.0:
            orig_frame_rate = audio.frame_rate
            slow_frame_rate = int(orig_frame_rate * speed_factor)
            audio_changed = audio._spawn(audio.raw_data, overrides={"frame_rate": slow_frame_rate})
        else:
            audio_changed = audio

        if pitch_factor != 1.0:
            semitones = 12 * (pitch_factor - 1)
            audio_changed = audio_changed._spawn(
                audio_changed.raw_data,
                overrides={"frame_rate": int(audio_changed.frame_rate * (2.0 ** (semitones / 12.0)))},
            ).set_frame_rate(audio_changed.frame_rate)

        audio_out_path = self.config.get("play_audio", "out_path") or "out"
        if not os.path.isabs(audio_out_path) and not audio_out_path.startswith("./"):
            audio_out_path = "./" + audio_out_path
        temp_path = self.common.get_new_audio_path(audio_out_path, f"temp_{self.common.get_bj_time(4)}.wav")
        audio_changed.export(temp_path, format="wav")
        return os.path.abspath(temp_path)

    async def only_play_audio(self):
        try:
            if self.config.get("play_audio", "player") == "pygame" and Audio.mixer_normal is not None:
                try:
                    Audio.mixer_normal.init()
                except Exception:
                    logger.error(traceback.format_exc())
                    logger.error("pygame mixer_normal 初始化失败")

            while True:
                with Audio.voice_tmp_path_queue_lock:
                    while not Audio.voice_tmp_path_queue:
                        Audio.voice_tmp_path_queue_not_empty.wait()
                    data_json = Audio.voice_tmp_path_queue.pop(0)

                voice_path = data_json.get("voice_path")
                if not voice_path or not os.path.exists(voice_path):
                    continue

                if not self.config.get("play_audio", "enable"):
                    await self.send_audio_play_info_to_callback()
                    continue

                player = self.config.get("play_audio", "player")
                if player in ["audio_player", "audio_player_v2"] and Audio.audio_player is not None:
                    Audio.audio_player.play({"file_path": voice_path})
                    await asyncio.sleep(float(self.config.get("play_audio", "normal_interval_min") or 0.2))
                elif player == "pygame" and Audio.mixer_normal is not None:
                    Audio.mixer_normal.music.load(voice_path)
                    Audio.mixer_normal.music.play()
                    while Audio.mixer_normal.music.get_busy():
                        await asyncio.sleep(0.1)
                    Audio.mixer_normal.music.stop()
                else:
                    await asyncio.sleep(0.1)

                await self.send_audio_play_info_to_callback()
                await asyncio.sleep(float(self.config.get("play_audio", "normal_interval_min") or 0.2))
        except Exception:
            logger.error(traceback.format_exc())

    def pause_copywriting_play(self):
        Audio.copywriting_play_flag = 0

    def unpause_copywriting_play(self):
        Audio.copywriting_play_flag = 2

    def stop_copywriting_play(self):
        Audio.copywriting_play_flag = 0

    async def copywriting_synthesis_audio(self, file_path, out_audio_path="out/", audio_synthesis_type="edge-tts"):
        try:
            max_len = self.config.get("filter", "max_len")
            max_char_len = self.config.get("filter", "max_char_len")
            content = self.common.read_file_return_content(str(file_path))
            content = self.common.remove_extra_words(content, max_len, max_char_len).replace("\n", "。")
            voice_result = await self.tts_handle(
                {
                    "type": "copywriting",
                    "tts_type": audio_synthesis_type,
                    "data": self.config.get(audio_synthesis_type),
                    "content": content,
                }
            )
            source_path = voice_result.get("voice_path")
            if not source_path:
                return None
            output_dir = Path(out_audio_path)
            if not output_dir.is_absolute():
                output_dir = Path.cwd() / output_dir
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{Path(file_path).stem}{Path(source_path).suffix}"
            Path(source_path).replace(output_path)
            return str(output_path)
        except Exception:
            logger.error(traceback.format_exc())
            return None

    def abnormal_alarm_handle(self, type):
        try:
            Audio.abnormal_alarm_data[type]["error_count"] += 1
            if not self.config.get("abnormal_alarm", type, "enable"):
                return True

            if self.config.get("abnormal_alarm", type, "type") == "local_audio":
                if Audio.abnormal_alarm_data[type]["error_count"] >= self.config.get("abnormal_alarm", type, "auto_restart_error_num"):
                    webui_ip = "127.0.0.1" if self.config.get("webui", "ip") == "0.0.0.0" else self.config.get("webui", "ip")
                    self.common.send_request(
                        f"http://{webui_ip}:{self.config.get('webui', 'port')}/sys_cmd",
                        "POST",
                        {"type": "restart", "api_type": "api", "data": {"config_path": "config.json"}},
                    )
            return True
        except Exception:
            logger.error(traceback.format_exc())
            return False
