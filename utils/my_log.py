# import sys
# from loguru import logger

# from utils.common import Common
# from utils.config import Config


# # 配置文件路径
# config_path = 'config.json'
# common = Common()

# logger.debug("配置文件路径=" + str(config_path))

# # 实例化配置类
# config = Config(config_path)

# # 获取当前时间并生成日志文件路径
# file_path = "./log/log-" + common.get_bj_time(1) + ".txt"

# # 配置 logger
# def configure_logger(file_path, log_level, max_file_size):
#     level = log_level.upper() if log_level else "INFO"
#     max_size = max_file_size if max_file_size else "1024 MB"

#     # 清空之前的handlers
#     logger.remove()

#     # 配置控制台输出
#     if level == "INFO":
#         logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss.SSS} | <lvl>{level:8}</>| <lvl>{message}</>", colorize=True, level=level)

#     # 配置文件输出
#     logger.add(file_path, level=level, rotation=max_size)

# # 获取日志配置
# log_level = config["webui"]["log"].get("log_level", "INFO")
# max_file_size = config["webui"]["log"].get("max_file_size", "1024 MB")

# # 配置 logger
# configure_logger(file_path, log_level, max_file_size)

# # 导出 logger 供其他模块使用
# __all__ = ["logger"]


import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)

try:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
except Exception:
    config = {"webui": {"log": {"log_level": "INFO", "max_file_size": "1024 MB"}}}

file_path = str(LOG_DIR / f"log-{datetime.now().strftime('%Y-%m-%d')}.txt")

# 配置 logger
def configure_logger(file_path, log_level, max_file_size):
    level = log_level.upper() if log_level else "INFO"
    max_size = max_file_size if max_file_size else "1024 MB"

    # 清空之前的handlers
    logger.remove()

    # 配置控制台输出
    # logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss.SSS} | <lvl>{level:8}</>| <lvl>{message}</>", colorize=True, level=level)
    logger.add(sys.stderr, colorize=True, level=level)


    # 配置文件输出
    logger.add(file_path, level=level, rotation=max_size)

# 获取日志配置
log_level = config.get("webui", {}).get("log", {}).get("log_level", "INFO")
max_file_size = config.get("webui", {}).get("log", {}).get("max_file_size", "1024 MB")

# 配置 logger
configure_logger(file_path, log_level, max_file_size)

# 获取 jieba 库的日志记录器，并设置其级别为 WARNING
jieba_logger = logging.getLogger("jieba")
jieba_logger.setLevel(logging.WARNING)

# 获取 httpx 库的日志记录器
httpx_logger = logging.getLogger("httpx")
# 设置 httpx 日志记录器的级别为 WARNING
httpx_logger.setLevel(logging.WARNING)

# 获取特定库的日志记录器
watchfiles_logger = logging.getLogger("watchfiles")
# 设置日志级别为WARNING或更高，以屏蔽INFO级别的日志消息
watchfiles_logger.setLevel(logging.WARNING)

# 获取 werkzeug 库的日志记录器
werkzeug_logger = logging.getLogger("werkzeug")
# 设置 httpx 日志记录器的级别为 WARNING
werkzeug_logger.setLevel(logging.WARNING)

# 将 loguru 与标准 logging 结合
class InterceptHandler(logging.Handler):
    def emit(self, record):
        loguru_logger = logger.bind(name=record.name)
        level = logger.level(record.levelname).name
        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_globals["__name__"] != __name__:
            frame = frame.f_back
            depth += 1
        loguru_logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

# 将 InterceptHandler 添加到 jieba logger
jieba_logger.addHandler(InterceptHandler())
# 将 InterceptHandler 添加到 httpx logger
httpx_logger.addHandler(InterceptHandler())
watchfiles_logger.addHandler(InterceptHandler())
werkzeug_logger.addHandler(InterceptHandler())

# 导出 logger 供其他模块使用
__all__ = ["logger"]
