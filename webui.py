import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path

import requests
from nicegui import app, ui

from utils.audio import Audio
from utils.common import Common
from utils.config import Config
from utils.models import SetConfigMessage, SysCmdMessage
from utils.my_log import logger


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOCAL_CONFIG_PATH = BASE_DIR / "config.local.json"
RUNTIME_DIR = BASE_DIR / ".runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

common = Common()
config = Config(str(CONFIG_PATH))
audio = Audio(str(CONFIG_PATH), type=2)

main_process = None
main_process_lock = threading.Lock()
log_messages = []
log_lock = threading.Lock()
log_area = None
status_label = None
memory_status_area = None
config_inputs = {}

SENSITIVE_KEYWORDS = (
    "password",
    "secret",
    "token",
    "cookie",
    "api_key",
    "apikey",
    "access_key",
    "auth_code",
    "bili_jct",
    "sessdata",
)


def mask_sensitive_value(value: str) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}***{text[-3:]}"


def is_sensitive_path(path: tuple[str, ...]) -> bool:
    joined = ".".join(path).lower()
    return any(keyword in joined for keyword in SENSITIVE_KEYWORDS)


def get_runtime_python() -> str:
    if os.name == "nt":
        venv_python = BASE_DIR / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = BASE_DIR / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable or "python"


def add_log(message: str) -> None:
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}"
    logger.info(message)
    with log_lock:
        log_messages.append(line)
        del log_messages[:-300]
        content = "\n".join(log_messages)
    if log_area is not None:
        log_area.value = content
        log_area.update()


def get_main_pid_file() -> Path:
    return RUNTIME_DIR / "main.pid"


def write_main_pid(pid: int) -> None:
    get_main_pid_file().write_text(str(pid), encoding="utf-8")


def read_main_pid() -> int | None:
    pid_file = get_main_pid_file()
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def clear_main_pid() -> None:
    pid_file = get_main_pid_file()
    if pid_file.exists():
        pid_file.unlink()


def is_pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def is_main_running() -> bool:
    global main_process
    with main_process_lock:
        if main_process is not None and main_process.poll() is None:
            return True
    return is_pid_running(read_main_pid())


def refresh_status() -> None:
    if status_label is None:
        return
    status_label.text = "运行中" if is_main_running() else "已停止"
    status_label.update()
    refresh_memory_status()


def load_base_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_local_config() -> dict:
    if not LOCAL_CONFIG_PATH.exists():
        return {}
    return json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def set_nested(data: dict, path: tuple[str, ...], value):
    current = data
    for key in path[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[path[-1]] = value


def get_nested(data: dict, path: tuple[str, ...], default=None):
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def coerce_value(value, value_type: str):
    if value_type == "bool":
        return bool(value)
    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    if value_type == "list":
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        return list(value or [])
    return "" if value is None else str(value)


def register_input(path: tuple[str, ...], widget, value_type: str = "str", local: bool = False):
    config_inputs[path] = {"widget": widget, "type": value_type, "local": local}


def save_config() -> None:
    base_config = load_base_config()
    local_config = load_local_config()

    for path, meta in config_inputs.items():
        widget = meta["widget"]
        value = coerce_value(widget.value, meta["type"])
        if meta["local"]:
            set_nested(local_config, path, value)
        else:
            set_nested(base_config, path, value)

    save_json(CONFIG_PATH, base_config)
    save_json(LOCAL_CONFIG_PATH, local_config)
    add_log("配置已保存")


def main_api_url(path: str) -> str:
    return f"http://127.0.0.1:{config.get('api_port')}{path}"


def notify_main_reload_config() -> None:
    if not is_main_running():
        return
    try:
        resp = requests.post(main_api_url("/reload_config"), timeout=15)
        add_log(f"主进程重载配置: {resp.text[:120]}")
    except Exception as exc:
        add_log(f"通知主进程重载配置失败: {exc}")


def read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def format_memory_status(data: dict) -> str:
    live_session = data.get("live_session", {})
    user_memory = data.get("user_memory", {})
    long_term_memory = data.get("long_term_memory", {})
    lines = [
        f"本场开始: {live_session.get('started_at') or '-'}",
        f"本场更新: {live_session.get('updated_at') or '-'}",
        f"本场弹幕: {live_session.get('total_comments', 0)}",
        f"本场活跃观众数: {live_session.get('active_user_count', 0)}",
        f"本场话题数: {live_session.get('topic_count', 0)}",
        f"用户记忆人数: {user_memory.get('user_count', 0)}",
        f"长期记忆场次: {long_term_memory.get('archive_count', 0)}",
        f"长期最新摘要: {long_term_memory.get('latest_summary') or '-'}",
        f"用户记忆文件: {user_memory.get('path') or (BASE_DIR / 'data' / 'lingya_memory.json')}",
        f"长期记忆文件: {long_term_memory.get('path') or (BASE_DIR / 'data' / 'lingya_long_term_memory.json')}",
    ]
    return "\n".join(lines)


def get_memory_status_payload() -> dict:
    if is_main_running():
        try:
            resp = requests.get(main_api_url("/memory_status"), timeout=15)
            payload = resp.json()
            if isinstance(payload, dict) and payload.get("data"):
                return payload["data"]
        except Exception:
            pass
    live_session = read_json_file(BASE_DIR / "data" / "lingya_live_session.json")
    user_memory = read_json_file(BASE_DIR / "data" / "lingya_memory.json")
    long_term_memory = read_json_file(BASE_DIR / "data" / "lingya_long_term_memory.json")
    return {
        "live_session": {
            "started_at": live_session.get("started_at"),
            "updated_at": live_session.get("updated_at"),
            "total_comments": int(live_session.get("total_comments", 0) or 0),
            "active_user_count": len(live_session.get("active_users") or {}),
            "topic_count": len(live_session.get("topic_counts") or {}),
        },
        "user_memory": {
            "user_count": len(user_memory) if isinstance(user_memory, dict) else 0,
            "path": str(BASE_DIR / "data" / "lingya_memory.json"),
        },
        "long_term_memory": {
            "archive_count": len((long_term_memory or {}).get("archives") or []) if isinstance(long_term_memory, dict) else 0,
            "latest_summary": (long_term_memory or {}).get("latest_summary") if isinstance(long_term_memory, dict) else "",
            "path": str(BASE_DIR / "data" / "lingya_long_term_memory.json"),
        },
    }


def refresh_memory_status() -> None:
    if memory_status_area is None:
        return
    try:
        memory_status_area.value = format_memory_status(get_memory_status_payload())
    except Exception as exc:
        memory_status_area.value = f"记忆状态读取失败: {exc}"
    memory_status_area.update()


def compress_memory(reset_session: bool = False, quiet: bool = False) -> None:
    if not is_main_running():
        if not quiet:
            add_log("主进程未运行，无法压缩本场记忆")
        refresh_memory_status()
        return
    try:
        resp = requests.post(main_api_url("/compress_memory"), json={"reset_session": reset_session}, timeout=30)
        payload = resp.json() if resp.content else {}
        if not quiet:
            add_log(f"记忆压缩结果: {payload}")
    except Exception as exc:
        if not quiet:
            add_log(f"记忆压缩失败: {exc}")
    refresh_memory_status()


def reload_runtime_config() -> None:
    global config, audio
    config = Config(str(CONFIG_PATH))
    audio.reload_config(str(CONFIG_PATH))


def start_main() -> None:
    global main_process
    with main_process_lock:
        if main_process is not None and main_process.poll() is None:
            add_log("main.py 已在运行")
            refresh_status()
            return

        pid = read_main_pid()
        if is_pid_running(pid):
            add_log(f"检测到 main.py 仍在运行，PID={pid}")
            refresh_status()
            return

        python_exe = get_runtime_python()
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        main_process = subprocess.Popen(
            [python_exe, "main.py"],
            cwd=str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        write_main_pid(main_process.pid)
        add_log(f"已启动 main.py，PID={main_process.pid}")
    refresh_status()


def ensure_main_running() -> None:
    if read_main_pid() and is_pid_running(read_main_pid()):
        return
    start_main()


def stop_main() -> None:
    global main_process
    pid = read_main_pid()
    if is_main_running() and bool(get_nested(config.config, ("memory", "auto_compress_on_stop"), True)):
        compress_memory(reset_session=False, quiet=False)
    with main_process_lock:
        proc = main_process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
                add_log("main.py 已停止")
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        elif is_pid_running(pid):
            try:
                os.kill(int(pid), signal.SIGTERM)
                add_log(f"已向 main.py 发送停止信号，PID={pid}")
            except Exception as exc:
                add_log(f"停止 main.py 失败: {exc}")
        main_process = None
        clear_main_pid()
    refresh_status()


def restart_main() -> None:
    stop_main()
    time.sleep(1)
    start_main()


def run_tts_test():
    save_config()
    reload_runtime_config()
    payload = {
        "type": "comment",
        "tts_type": "edge-tts",
        "data": config.get("edge-tts"),
        "config": config.get("filter"),
        "username": "测试",
        "content": "[[CUTE]] 铃芽在报到。",
        "voice_mode": "cute",
    }
    try:
        result = asyncio.run(audio.tts_handle(payload))
        add_log(f"TTS 测试完成: {result.get('voice_path') if isinstance(result, dict) else result}")
    except Exception as exc:
        add_log(f"TTS 测试失败: {exc}")


def run_llm_test():
    save_config()
    payload = {"type": "comment", "username": "测试", "content": "晚上好"}
    try:
        resp = requests.post(f"http://127.0.0.1:{config.get('api_port')}/llm", json=payload, timeout=30)
        add_log(f"LLM 测试: {resp.text[:200]}")
    except Exception as exc:
        add_log(f"LLM 测试失败: {exc}")


def run_public_reply_test():
    save_config()
    payload = {"type": "comment", "data": {"platform": "bilibili2", "username": "测试员", "content": "测试公屏回复"}}
    try:
        resp = requests.post(f"http://127.0.0.1:{config.get('api_port')}/send", json=payload, timeout=30)
        add_log(f"发送测试弹幕事件: {resp.text[:200]}")
    except Exception as exc:
        add_log(f"发送测试弹幕事件失败: {exc}")


@app.post("/set_config")
async def set_config_api(msg: SetConfigMessage):
    try:
        save_json(CONFIG_PATH, msg.data)
        add_log("收到 set_config，已保存 config.json")
        return {"code": 200, "message": "ok"}
    except Exception as exc:
        add_log(f"set_config 失败: {exc}")
        return {"code": -1, "message": str(exc)}


@app.post("/sys_cmd")
async def sys_cmd_api(msg: SysCmdMessage):
    try:
        cmd_type = msg.type
        if cmd_type == "start":
            start_main()
        elif cmd_type == "stop":
            stop_main()
        elif cmd_type == "restart":
            restart_main()
        else:
            add_log(f"忽略未知 sys_cmd: {cmd_type}")
        return {"code": 200, "message": "ok"}
    except Exception as exc:
        add_log(f"sys_cmd 失败: {exc}")
        return {"code": -1, "message": str(exc)}


@app.post("/callback")
async def callback_api(payload: dict):
    try:
        data = payload.get("data", {})
        content_type = data.get("content_type")
        prefix = data.get("type") or payload.get("type") or "callback"
        content = data.get("content") or ""
        username = data.get("username") or ""
        if content_type == "public_reply_log":
            add_log(f"公屏发送成功: {content}")
        elif content_type == "public_reply_error":
            add_log(f"公屏发送失败: {content}")
        elif username:
            add_log(f"{prefix} {username}: {content}")
        else:
            add_log(f"{prefix}: {content}")
        return {"code": 200, "message": "ok"}
    except Exception as exc:
        add_log(f"callback 处理失败: {exc}")
        return {"code": -1, "message": str(exc)}


def build_input(label: str, path: tuple[str, ...], value_type: str = "str", local: bool = False, textarea: bool = False, placeholder: str = ""):
    value = get_nested(config.config, path, "")
    if value_type == "list":
        value = "\n".join(value or [])
    sensitive = is_sensitive_path(path)
    if textarea:
        widget = ui.textarea(label=label, value=value, placeholder=placeholder).props("outlined autogrow")
    else:
        widget = ui.input(
            label=label,
            value=value,
            placeholder=placeholder,
            password=sensitive,
            password_toggle_button=sensitive,
        ).props("outlined")
    register_input(path, widget, value_type=value_type, local=local)
    return widget


def build_switch(label: str, path: tuple[str, ...], local: bool = False):
    widget = ui.switch(label, value=bool(get_nested(config.config, path, False)))
    register_input(path, widget, value_type="bool", local=local)
    return widget


def build_number(label: str, path: tuple[str, ...], value_type: str = "int", local: bool = False):
    value = get_nested(config.config, path, 0)
    widget = ui.number(label=label, value=value).props("outlined")
    register_input(path, widget, value_type=value_type, local=local)
    return widget


def build_ui():
    global log_area, status_label, memory_status_area

    ui.page_title("AI-Vtuber 精简控制台")
    ui.query("body").style("background:#f5f1e8;")

    with ui.column().classes("w-full max-w-6xl mx-auto gap-4 p-4"):
        with ui.card().classes("w-full"):
            ui.label("AI-Vtuber").classes("text-2xl")
            with ui.row().classes("items-center gap-4"):
                status_label = ui.label("未知")
                ui.button("保存配置", on_click=lambda: [save_config(), reload_runtime_config(), notify_main_reload_config(), refresh_memory_status()]).props("color=primary")
                ui.button("启动主程序", on_click=start_main).props("color=positive")
                ui.button("停止主程序", on_click=stop_main).props("color=negative")
                ui.button("重启主程序", on_click=restart_main)
                ui.button("TTS 测试", on_click=run_tts_test)
                ui.button("LLM 测试", on_click=run_llm_test)
                ui.button("模拟弹幕事件", on_click=run_public_reply_test)

        with ui.tabs().classes("w-full") as tabs:
            basic_tab = ui.tab("基础")
            persona_tab = ui.tab("人设")
            bili_tab = ui.tab("B站")
            zhipu_tab = ui.tab("智谱")
            tts_tab = ui.tab("语音")
            behavior_tab = ui.tab("行为")
            memory_tab = ui.tab("记忆")
            log_tab = ui.tab("日志")

        with ui.tab_panels(tabs, value=basic_tab).classes("w-full"):
            with ui.tab_panel(basic_tab):
                with ui.grid(columns=2).classes("w-full gap-4"):
                    build_input("API IP", ("api_ip",))
                    build_number("API 端口", ("api_port",), "int")
                    build_input("平台", ("platform",), local=True)
                    build_input("直播间 ID", ("room_display_id",), local=True)
                    build_input("聊天模型", ("chat_type",))
                    build_input("需要语言", ("need_lang",))
                    build_input("音频合成类型", ("audio_synthesis_type",))
                    build_input("视觉体", ("visual_body",))

            with ui.tab_panel(persona_tab):
                ui.label("CUTE / REAL 使用独立提示词块。")
                build_input("前置提示词", ("before_prompt",), textarea=True)
                build_input("后置提示词", ("after_prompt",), textarea=True)
                build_input("CUTE 提示词", ("persona_modes", "cute", "prompt"), textarea=True)
                build_input("REAL 提示词", ("persona_modes", "real", "prompt"), textarea=True)

            with ui.tab_panel(bili_tab):
                with ui.grid(columns=2).classes("w-full gap-4"):
                    build_input("B站登录方式", ("bilibili", "login_type"), local=True)
                    build_input("B站 Cookie", ("bilibili", "cookie"), local=True, textarea=True, placeholder=mask_sensitive_value(get_nested(config.config, ("bilibili", "cookie"), "")))
                    build_input("ac_time_value", ("bilibili", "ac_time_value"), local=True)
                    build_switch("启用公屏回复", ("bilibili_send_reply", "enable"), local=True)
                    build_number("公屏回复字数上限", ("bilibili_send_reply", "max_len"), "int")

            with ui.tab_panel(zhipu_tab):
                with ui.grid(columns=2).classes("w-full gap-4"):
                    build_input("模型", ("zhipu", "model"))
                    build_input("API Key", ("zhipu", "api_key"), local=True)
                    build_number("temperature", ("zhipu", "temperature"), "float")
                    build_number("top_p", ("zhipu", "top_p"), "float")
                    build_switch("保留上下文", ("zhipu", "history_enable"))
                    build_number("历史长度", ("zhipu", "history_max_len"), "int")
                    build_switch("流式", ("zhipu", "stream"))

            with ui.tab_panel(tts_tab):
                with ui.grid(columns=2).classes("w-full gap-4"):
                    build_switch("启用播放", ("play_audio", "enable"))
                    build_input("播放器", ("play_audio", "player"))
                    build_input("输出目录", ("play_audio", "out_path"))
                    build_switch("拆分长文本", ("play_audio", "text_split_enable"))
                    build_number("播放最小间隔", ("play_audio", "normal_interval_min"), "float")
                    build_number("播放最大间隔", ("play_audio", "normal_interval_max"), "float")
                    build_input("Cute 声线", ("edge-tts", "modes", "cute", "voice"))
                    build_input("Cute 语速", ("edge-tts", "modes", "cute", "rate"))
                    build_input("Cute 音量", ("edge-tts", "modes", "cute", "volume"))
                    build_input("Cute 音高", ("edge-tts", "modes", "cute", "pitch"))
                    build_input("Real 声线", ("edge-tts", "modes", "real", "voice"))
                    build_input("Real 语速", ("edge-tts", "modes", "real", "rate"))
                    build_input("Real 音量", ("edge-tts", "modes", "real", "volume"))
                    build_input("Real 音高", ("edge-tts", "modes", "real", "pitch"))

            with ui.tab_panel(behavior_tab):
                with ui.expansion("读弹幕", value=True).classes("w-full"):
                    with ui.grid(columns=2).classes("w-full gap-4"):
                        build_switch("启用读弹幕", ("read_comment", "enable"))
                        build_switch("读用户名", ("read_comment", "read_username_enable"))
                        build_number("用户名长度", ("read_comment", "username_max_len"), "int")
                        build_input("读用户名模板", ("read_comment", "read_username_copywriting"), value_type="list", textarea=True)
                with ui.expansion("过滤", value=True).classes("w-full"):
                    with ui.grid(columns=2).classes("w-full gap-4"):
                        build_input("前缀白名单", ("filter", "before_must_str"), value_type="list", textarea=True)
                        build_input("后缀白名单", ("filter", "after_must_str"), value_type="list", textarea=True)
                        build_input("前缀过滤", ("filter", "before_filter_str"), value_type="list", textarea=True)
                        build_input("后缀过滤", ("filter", "after_filter_str"), value_type="list", textarea=True)
                        build_switch("过滤表情", ("filter", "emoji"))
                        build_number("最大长度", ("filter", "max_len"), "int")
                        build_number("最大字符数", ("filter", "max_char_len"), "int")
                        build_switch("脏词替换", ("filter", "badwords", "enable"))
                        build_switch("脏词丢弃", ("filter", "badwords", "discard"))
                        build_input("脏词文件", ("filter", "badwords", "path"))
                        build_input("拼音脏词文件", ("filter", "badwords", "bad_pinyin_path"))
                        build_input("脏词替换字符", ("filter", "badwords", "replace"))
                with ui.expansion("欢迎与感谢", value=True).classes("w-full"):
                    with ui.grid(columns=2).classes("w-full gap-4"):
                        build_switch("欢迎入场", ("thanks", "entrance_enable"))
                        build_switch("礼物感谢", ("thanks", "gift_enable"))
                        build_switch("关注感谢", ("thanks", "follow_enable"))
                        build_number("用户名长度", ("thanks", "username_max_len"), "int")
                        build_input("入场欢迎文案", ("thanks", "entrance_copy"), value_type="list", textarea=True)
                        build_input("礼物感谢文案", ("thanks", "gift_copy"), value_type="list", textarea=True)
                        build_input("关注感谢文案", ("thanks", "follow_copy"), value_type="list", textarea=True)
                        build_switch("特殊欢迎启用", ("thanks", "special_entrance", "enable"))
                        build_switch("每场首次欢迎", ("thanks", "special_entrance", "only_first_time_per_live"))
                        build_number("熟观众最低进场数", ("thanks", "special_entrance", "familiar_viewer", "min_entrance_count"), "int")
                        build_input("熟观众欢迎文案", ("thanks", "special_entrance", "familiar_viewer", "copy"), value_type="list", textarea=True)
                        build_number("高牌子最低等级", ("thanks", "special_entrance", "high_medal", "min_medal_level"), "int")
                        build_input("高牌子欢迎文案", ("thanks", "special_entrance", "high_medal", "copy"), value_type="list", textarea=True)
                        build_number("舰长最低等级", ("thanks", "special_entrance", "guard", "min_guard_level"), "int")
                        build_input("舰长欢迎文案", ("thanks", "special_entrance", "guard", "copy"), value_type="list", textarea=True)
                with ui.expansion("排队", value=True).classes("w-full"):
                    with ui.grid(columns=2).classes("w-full gap-4"):
                        build_switch("启用排队", ("queue", "enable"))
                        build_number("最大人数", ("queue", "max_size"), "int")
                        build_number("显示人数", ("queue", "show_limit"), "int")
                        build_number("备注长度", ("queue", "note_max_len"), "int")
                        build_switch("所有人可管理", ("queue", "allow_all_users_manage"))
                        build_input("管理员", ("queue", "admin_usernames"), value_type="list", textarea=True)
                        build_input("加入命令", ("queue", "join_cmd"), value_type="list", textarea=True)
                        build_input("离开命令", ("queue", "leave_cmd"), value_type="list", textarea=True)
                        build_input("查询自己命令", ("queue", "my_status_cmd"), value_type="list", textarea=True)
                        build_input("查看队列命令", ("queue", "list_cmd"), value_type="list", textarea=True)
                        build_input("下一位命令", ("queue", "next_cmd"), value_type="list", textarea=True)
                        build_input("清空队列命令", ("queue", "clear_cmd"), value_type="list", textarea=True)
                with ui.expansion("喋喋不休", value=True).classes("w-full"):
                    with ui.grid(columns=2).classes("w-full gap-4"):
                        build_switch("启用喋喋不休", ("chatter_mode", "enable"))
                        build_switch("默认开启", ("chatter_mode", "default_on"))
                        build_input("触发用户名", ("chatter_mode", "trigger_usernames"), value_type="list", textarea=True)
                        build_input("开启命令", ("chatter_mode", "start_cmd"))
                        build_input("关闭命令", ("chatter_mode", "stop_cmd"))
                        build_number("最小间隔", ("chatter_mode", "interval_min"), "int")
                        build_number("最大间隔", ("chatter_mode", "interval_max"), "int")
                        build_input("兜底文案", ("chatter_mode", "fallback_copy"), value_type="list", textarea=True)
                        build_input("开启回复", ("chatter_mode", "start_reply"))
                        build_input("关闭回复", ("chatter_mode", "stop_reply"))
                with ui.expansion("定时任务", value=False).classes("w-full"):
                    ui.label("精简版保留配置，不在这里做复杂编辑。需要改动时直接改 JSON 更稳。")
                with ui.expansion("闲时任务", value=False).classes("w-full"):
                    ui.label("精简版保留配置，不在这里做复杂编辑。需要改动时直接改 JSON 更稳。")

            with ui.tab_panel(memory_tab):
                ui.label(f"用户记忆文件: {BASE_DIR / 'data' / 'lingya_memory.json'}")
                ui.label(f"本场记忆文件: {BASE_DIR / 'data' / 'lingya_live_session.json'}")
                ui.label(f"长期记忆文件: {BASE_DIR / 'data' / 'lingya_long_term_memory.json'}")
                with ui.row().classes("items-center gap-4"):
                    ui.button("刷新记忆状态", on_click=refresh_memory_status)
                    ui.button("压缩本场记忆", on_click=lambda: compress_memory(reset_session=False)).props("color=primary")
                    ui.button("压缩并开始新场", on_click=lambda: compress_memory(reset_session=True)).props("color=secondary")
                memory_status_area = ui.textarea(label="记忆状态", value="", placeholder="这里会显示本场与长期记忆状态").props("outlined autogrow readonly")
                memory_status_area.classes("w-full")
                with ui.grid(columns=2).classes("w-full gap-4"):
                    build_switch("启用记忆", ("memory", "enable"))
                    build_number("每人最大笔记数", ("memory", "max_notes_per_user"), "int")
                    build_number("注入提示词条数", ("memory", "max_prompt_notes"), "int")
                    build_number("单条笔记字数", ("memory", "note_max_len"), "int")
                    build_switch("启用本场直播记忆", ("memory", "live_session_enable"))
                    build_number("本场近期弹幕注入条数", ("memory", "live_recent_prompt_limit"), "int")
                    build_number("本场活跃观众注入人数", ("memory", "live_active_user_limit"), "int")
                    build_number("本场高频话题注入条数", ("memory", "live_topic_limit"), "int")
                    build_number("本场最多缓存弹幕数", ("memory", "live_store_max_comments"), "int")
                    build_switch("启用长期记忆", ("memory", "long_term_enable"))
                    build_number("长期记忆注入场次数", ("memory", "long_term_prompt_limit"), "int")
                    build_number("长期记忆归档上限", ("memory", "long_term_archive_limit"), "int")
                    build_number("压缩摘要最大字数", ("memory", "summary_max_len"), "int")
                    build_number("压缩时保留近期弹幕数", ("memory", "summary_recent_comments_limit"), "int")
                    build_number("压缩时保留活跃观众数", ("memory", "summary_top_users_limit"), "int")
                    build_number("压缩时保留高频话题数", ("memory", "summary_top_topics_limit"), "int")
                    build_switch("停止主程序前自动压缩", ("memory", "auto_compress_on_stop"))

            with ui.tab_panel(log_tab):
                log_area = ui.textarea(label="运行日志", value="", placeholder="这里会显示 WebUI、本地回调、公屏发送结果").props("outlined autogrow readonly")
                log_area.classes("w-full")

    refresh_status()
    refresh_memory_status()


build_ui()
ensure_main_running()
ui.timer(2.0, refresh_status)


if __name__ in {"__main__", "__mp_main__"}:
    add_log("WebUI 已启动")
    ui.run(host=config.get("webui", "ip"), port=int(config.get("webui", "port")), reload=False, show=True)
