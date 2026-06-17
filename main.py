import asyncio
import http.server
import os
import random
import signal
import socketserver
import threading
import time
import traceback
from functools import partial

import schedule

from utils.common import Common
from utils.config import Config
from utils.my_handle import My_handle
from utils.my_log import logger
import utils.my_global as my_global


config = None
common = None
my_handle = None
config_path = "config.json"


async def web_server_thread(web_server_port):
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", web_server_port), handler) as httpd:
        logger.info(f"Web运行在端口：{web_server_port}")
        logger.info(f"可以直接访问Live2D页， http://127.0.0.1:{web_server_port}/Live2D/")
        httpd.serve_forever()


def start_server():
    global config, common, my_handle, config_path

    my_global.wait_play_audio_num = 0
    my_global.wait_synthesis_msg_num = 0
    my_global.last_liveroom_data = {
        "OnlineUserCount": 0,
        "TotalUserCount": 0,
        "TotalUserCountStr": "0",
        "OnlineUserCountStr": "0",
        "MsgId": 0,
        "User": None,
        "Content": "当前直播间人数 0，累计直播间人数 0",
        "RoomId": 0,
    }
    my_global.last_username_list = [""]

    my_handle = My_handle(config_path)
    if my_handle is None:
        logger.error("程序初始化失败！")
        os._exit(0)

    try:
        if config.get("live2d", "enable"):
            web_server_port = int(config.get("live2d", "port"))
            threading.Thread(target=lambda: asyncio.run(web_server_thread(web_server_port)), daemon=True).start()
    except Exception:
        logger.error(traceback.format_exc())
        os._exit(0)

    def http_api_thread():
        import uvicorn
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
        from starlette.requests import Request

        from utils.models import CallbackMessage, CommonResult, LLMMessage, SendMessage

        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.post("/send")
        async def send(msg: SendMessage):
            try:
                tmp_json = msg.dict()
                data_json = tmp_json["data"]
                if "type" not in data_json:
                    data_json["type"] = tmp_json["type"]
                if data_json["type"] in ["reread", "reread_top_priority"]:
                    my_handle.reread_handle(data_json, type=data_json["type"])
                elif data_json["type"] == "comment":
                    my_handle.process_data(data_json, "comment")
                elif data_json["type"] == "gift":
                    my_handle.gift_handle(data_json)
                elif data_json["type"] == "entrance":
                    my_handle.entrance_handle(data_json)
                return CommonResult(code=200, message="成功")
            except Exception as e:
                logger.error(f"发送数据失败！{e}")
                return CommonResult(code=-1, message=f"发送数据失败！{e}")

        @app.post("/llm")
        async def llm(msg: LLMMessage):
            try:
                data_json = msg.dict()
                resp_content = my_handle.llm_handle(data_json["type"], data_json, webui_show=False)
                return CommonResult(code=200, message="成功", data={"content": resp_content})
            except Exception as e:
                logger.error(f"调用LLM失败！{e}")
                return CommonResult(code=-1, message=f"调用LLM失败！{e}")

        @app.post("/tts")
        async def tts(request: Request):
            try:
                data_json = await request.json()
                resp_json = await My_handle.audio.tts_handle(data_json)
                return {"code": 200, "message": "成功", "data": resp_json}
            except Exception as e:
                logger.error(traceback.format_exc())
                return CommonResult(code=-1, message=f"失败！{e}")

        @app.post("/callback")
        async def callback(msg: CallbackMessage):
            try:
                data_json = msg.dict()
                if data_json["type"] == "audio_playback_completed":
                    my_global.wait_play_audio_num = int(data_json["data"]["wait_play_audio_num"])
                    my_global.wait_synthesis_msg_num = int(data_json["data"]["wait_synthesis_msg_num"])
                return CommonResult(code=200, message="callback处理成功！")
            except Exception as e:
                logger.error(f"callback处理失败！{e}")
                return CommonResult(code=-1, message=f"callback处理失败！{e}")

        @app.get("/memory_status")
        async def memory_status():
            try:
                data = my_handle.get_memory_status()
                return CommonResult(code=200, data=data, message="memory_status处理成功！")
            except Exception as e:
                logger.error(f"memory_status处理失败！{e}")
                return CommonResult(code=-1, message=f"memory_status处理失败！{e}")

        @app.post("/compress_memory")
        async def compress_memory(request: Request):
            try:
                payload = await request.json()
                result = my_handle.compress_live_session_memory(reset_session=bool(payload.get("reset_session", False)))
                return CommonResult(code=200, data=result, message="compress_memory处理成功！")
            except Exception as e:
                logger.error(traceback.format_exc())
                return CommonResult(code=-1, message=f"compress_memory处理失败！{e}")

        @app.post("/reload_config")
        async def reload_config_api():
            try:
                global config, platform
                config = Config(config_path)
                platform = config.get("platform")
                my_handle.reload_config(config_path, reset_live_session=False)
                return CommonResult(code=200, message="reload_config处理成功！")
            except Exception as e:
                logger.error(traceback.format_exc())
                return CommonResult(code=-1, message=f"reload_config处理失败！{e}")

        @app.get("/get_sys_info")
        async def get_sys_info():
            try:
                data = {
                    "audio": my_handle.get_audio_info(),
                    "metahuman-stream": {
                        "wait_play_audio_num": my_global.wait_play_audio_num,
                        "wait_synthesis_msg_num": my_global.wait_synthesis_msg_num,
                    },
                }
                return CommonResult(code=200, data=data, message="get_sys_info处理成功！")
            except Exception as e:
                logger.error(f"get_sys_info处理失败！{e}")
                return CommonResult(code=-1, message=f"get_sys_info处理失败！{e}")

        logger.info("HTTP API线程已启动！")
        uvicorn.run(app, host="0.0.0.0", port=config.get("api_port"))

    threading.Thread(target=http_api_thread, daemon=True).start()

    def schedule_task(index):
        hour, minute = common.get_bj_time(6)
        if 0 <= hour < 6:
            current_time = f"凌晨{hour}点{minute}分"
        elif 6 <= hour < 9:
            current_time = f"早晨{hour}点{minute}分"
        elif 9 <= hour < 12:
            current_time = f"上午{hour}点{minute}分"
        elif hour == 12:
            current_time = f"中午{hour}点{minute}分"
        elif 13 <= hour < 18:
            current_time = f"下午{hour - 12}点{minute}分"
        elif 18 <= hour < 20:
            current_time = f"傍晚{hour - 12}点{minute}分"
        else:
            current_time = f"晚上{hour - 12}点{minute}分"

        copies = config.get("schedule")[index]["copy"]
        if not copies:
            return
        random_copy = random.choice(copies)
        variables = {
            "time": current_time,
            "user_num": my_global.last_liveroom_data["OnlineUserCount"],
            "last_username": my_global.last_username_list[-1],
        }
        content = random_copy.format(**{k: v for k, v in variables.items() if f"{{{k}}}" in random_copy}) if any(f"{{{k}}}" in random_copy for k in variables) else random_copy
        content = common.brackets_text_randomize(content)
        my_handle.process_data({"platform": platform, "username": "定时任务", "content": content}, "schedule")

    def run_schedule():
        try:
            for index, task in enumerate(config.get("schedule")):
                if not task["enable"]:
                    continue

                def schedule_random_task(idx, min_seconds, max_seconds):
                    schedule.clear(idx)
                    next_time = random.randint(int(min_seconds), int(max_seconds))
                    schedule_task(idx)
                    schedule.every(next_time).seconds.do(schedule_random_task, idx, min_seconds, max_seconds).tag(idx)

                schedule_random_task(index, task["time_min"], task["time_max"])
        except Exception:
            logger.error(traceback.format_exc())

        while True:
            schedule.run_pending()
            time.sleep(0.2)

    if any(item["enable"] for item in config.get("schedule")):
        threading.Thread(target=run_schedule, daemon=True).start()

    async def idle_time_task():
        try:
            if not config.get("idle_time_task", "enable"):
                return

            logger.info("闲时任务线程运行中...")
            last_mode = 0
            copywriting_copy_list = list(config.get("idle_time_task", "copywriting", "copy") or [])
            comment_copy_list = list(config.get("idle_time_task", "comment", "copy") or [])
            local_audio_path_list = list(config.get("idle_time_task", "local_audio", "path") or [])
            overflow_time_min = int(config.get("idle_time_task", "idle_time_min"))
            overflow_time_max = int(config.get("idle_time_task", "idle_time_max"))
            overflow_time = random.randint(overflow_time_min, overflow_time_max) if overflow_time_min > 0 and overflow_time_max > 0 else 0

            def pop_item(items, random_mode):
                if not items:
                    return None
                if random_mode:
                    random.shuffle(items)
                return items.pop(0)

            while True:
                await asyncio.sleep(1 if overflow_time > 0 else 0.1)
                my_global.global_idle_time += 1
                if config.get("idle_time_task", "type") != "直播间无消息更新闲时":
                    continue
                if my_global.global_idle_time < overflow_time:
                    continue

                my_global.global_idle_time = 0
                overflow_time = random.randint(overflow_time_min, overflow_time_max) if overflow_time_min > 0 and overflow_time_max > 0 else 0
                if last_mode == 0 and config.get("idle_time_task", "copywriting", "enable"):
                    item = pop_item(copywriting_copy_list, config.get("idle_time_task", "copywriting", "random"))
                    if item is not None:
                        item = common.brackets_text_randomize(item)
                        my_handle.process_data({"platform": platform, "username": "闲时任务-文案模式", "type": "reread", "content": item}, "idle_time_task")
                    last_mode = 1
                    continue
                if last_mode == 1 and config.get("idle_time_task", "comment", "enable"):
                    item = pop_item(comment_copy_list, config.get("idle_time_task", "comment", "random"))
                    if item is not None:
                        item = common.brackets_text_randomize(item)
                        my_handle.process_data({"platform": platform, "username": "闲时任务-弹幕触发LLM模式", "type": "comment", "content": item}, "idle_time_task")
                    last_mode = 2
                    continue
                if config.get("idle_time_task", "local_audio", "enable"):
                    item = pop_item(local_audio_path_list, config.get("idle_time_task", "local_audio", "random"))
                    if item is not None:
                        item = common.brackets_text_randomize(item)
                        my_handle.process_data(
                            {
                                "platform": platform,
                                "username": "闲时任务-本地音频模式",
                                "type": "local_audio",
                                "content": common.extract_filename(item, False),
                                "file_path": item,
                            },
                            "idle_time_task",
                        )
                last_mode = 0
        except Exception:
            logger.error(traceback.format_exc())

    if config.get("idle_time_task", "enable"):
        threading.Thread(target=lambda: asyncio.run(idle_time_task()), daemon=True).start()

    logger.info(f"当前平台：{platform}")
    if platform != "bilibili2":
        logger.error(f"已精简版本仅支持平台: bilibili2，当前为 {platform}")
        return

    from utils.platforms.bilibili2 import start_listen

    start_listen(config, common, my_handle, platform)


def exit_handler(signum, frame):
    logger.info("收到信号: %s", signum)
    try:
        if my_handle is not None and config is not None:
            memory_config = config.get("memory") or {}
            if memory_config.get("auto_compress_on_stop", True):
                result = my_handle.compress_live_session_memory(reset_session=False)
                logger.info("退出前已压缩本场记忆: %s", result.get("summary"))
    except Exception:
        logger.error(traceback.format_exc())


if __name__ == "__main__":
    common = Common()
    config = Config(config_path)
    platform = config.get("platform")

    signal.signal(signal.SIGINT, exit_handler)
    signal.signal(signal.SIGTERM, exit_handler)

    start_server()
