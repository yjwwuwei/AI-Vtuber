# -*- coding: UTF-8 -*-
"""
@Project : AI-Vtuber
@File    : gpt.py
@Author  : HildaM
@Email   : Hilda_quan@163.com
@Date    : 2023/06/23 下午 7:47
@Description : 统一模型层抽象
"""
from utils.my_log import logger
from utils.gpt_model.zhipu import Zhipu


class GPT_Model:
    openai = None

    def set_model_config(self, model_name, config):
        if model_name == "zhipu":
            self.zhipu = Zhipu(config)
            return
        logger.warning(f"已精简，仅保留 zhipu，忽略模型配置: {model_name}")

    def set_vision_model_config(self, model_name, config):
        logger.warning(f"已精简，不再支持视觉模型配置: {model_name}")

    def get(self, name):
        logger.info("GPT_MODEL: 进入get方法")
        try:
            if name != "reread":
                return getattr(self, name)
        except AttributeError:
            logger.warning(f"{name} 该模型不支持，如果不是LLM的类型，那就只是个警告，可以正常使用，请放心")
            return None

    def get_openai_key(self):
        if self.openai is None:
            logger.error("openai_key 为空")
            return None
        return self.openai["api_key"]

    def get_openai_model_name(self):
        if self.openai is None:
            logger.warning("openai的model为空，将设置为默认gpt-3.5")
            return "gpt-3.5-turbo-0301"
        return self.openai["model"]


GPT_MODEL = GPT_Model()
