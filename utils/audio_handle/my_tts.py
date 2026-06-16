import os
import traceback

import edge_tts

from utils.common import Common
from utils.config import Config
from utils.my_log import logger


class MY_TTS:
    def __init__(self, config_path):
        self.common = Common()
        self.config = Config(config_path)
        self.audio_out_path = self.config.get("play_audio", "out_path") or "out"
        if not os.path.isabs(self.audio_out_path) and not self.audio_out_path.startswith("./"):
            self.audio_out_path = "./" + self.audio_out_path

    async def edge_tts_api(self, data):
        try:
            file_name = "edge_tts_" + self.common.get_bj_time(4) + ".mp3"
            voice_tmp_path = self.common.get_new_audio_path(self.audio_out_path, file_name)
            content = (data.get("content") or "").replace('"', "").replace("'", "")
            edge_tts_config = data.get("edge-tts") or {}
            proxy = edge_tts_config.get("proxy") or None
            communicate = edge_tts.Communicate(
                text=content,
                voice=edge_tts_config.get("voice", "zh-CN-XiaoxiaoNeural"),
                rate=edge_tts_config.get("rate", "+0%"),
                volume=edge_tts_config.get("volume", "+0%"),
                pitch=edge_tts_config.get("pitch", "+0Hz"),
                proxy=proxy,
            )
            await communicate.save(voice_tmp_path)
            return voice_tmp_path
        except Exception:
            logger.error(traceback.format_exc())
            return None

    async def vits_api(self, data):
        logger.warning("精简版未启用 vits")
        return None

    async def bert_vits2_api(self, data):
        logger.warning("精简版未启用 bert_vits2")
        return None

    def vits_fast_api(self, data):
        logger.warning("精简版未启用 vits_fast")
        return None

    def openai_tts_api(self, data):
        logger.warning("精简版未启用 openai_tts")
        return None

    def gradio_tts_api(self, data):
        logger.warning("精简版未启用 gradio_tts")
        return None

    async def gpt_sovits_api(self, data):
        logger.warning("精简版未启用 gpt_sovits")
        return None

    def azure_tts_api(self, data):
        logger.warning("精简版未启用 azure_tts")
        return None

    async def cosyvoice_api(self, data):
        logger.warning("精简版未启用 cosyvoice")
        return None

    async def f5_tts_api(self, data):
        logger.warning("精简版未启用 f5_tts")
        return None

    async def multitts_api(self, data):
        logger.warning("精简版未启用 multitts")
        return None

    async def melotts_api(self, data):
        logger.warning("精简版未启用 melotts")
        return None

    async def index_tts_api(self, data):
        logger.warning("精简版未启用 index_tts")
        return None
