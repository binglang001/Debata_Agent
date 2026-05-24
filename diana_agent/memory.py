"""对话记忆管理 —— 异步 I/O，支持小模型去重 + 大模型总结截断"""

import asyncio
import atexit
import json
import os

from nonebot.log import logger

from .behavior_prompt import BEHAVIOR_PROMPT, PRO_TOOLS_PROMPT
from .config import PERSONA as _PERSONA_NAME, SUMMARIZE_AT, SUMMARIZE_RANGE
from .persona_prompt import PERSONA_PROMPT, PERSONA_VARS
from .utils import get_time

_TEST_MODE = os.getenv("DIANA_TEST_MODE") == "true"
_PERSONA_DIR = os.path.join(os.path.dirname(__file__), "..", "personas", _PERSONA_NAME)

MEMORY_FILE = os.path.join(_PERSONA_DIR, "memory_test.json" if _TEST_MODE else "memory.json")
IMPORTANT_FILE = os.path.join(_PERSONA_DIR, "important_memory_test.json" if _TEST_MODE else "important_memory.json")

COMBINED_SYSTEM = BEHAVIOR_PROMPT.strip() + "\n\n" + PERSONA_PROMPT.strip() + "\n\n" + PRO_TOOLS_PROMPT.strip()

history: list[dict] = []
_lock = asyncio.Lock()
_important_memories_text: str = ""


# ========== 异步 I/O 辅助 ==========
async def _read_json(path: str, default=None):
    if default is None:
        default = []
    try:
        def _read():
            if not os.path.exists(path):
                return default
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return await asyncio.to_thread(_read)
    except (json.JSONDecodeError, TypeError, KeyError, Exception):
        logger.warning(f"{path} 读取失败，使用默认值")
        return default


async def _write_json(path: str, data) -> None:
    def _write():
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子替换
    await asyncio.to_thread(_write)


# ========== 持久化 ==========
async def save_history() -> None:
    await _write_json(MEMORY_FILE, history)


async def load_history() -> list[dict]:
    return await _read_json(MEMORY_FILE, [])


# ========== 重要记忆 ==========
async def _load_important_items() -> list[dict]:
    return await _read_json(IMPORTANT_FILE, [])


async def _save_important_items(items: list[dict]) -> None:
    await _write_json(IMPORTANT_FILE, items)


def load_important_memories_text() -> str:
    """同步读取缓存（调用方确保先调过 refresh_important_cache）"""
    return _important_memories_text


async def refresh_important_cache() -> None:
    global _important_memories_text
    items = await _load_important_items()
    if not items:
        _important_memories_text = ""
        return
    _important_memories_text = "[重要记忆]\n" + "\n".join(
        f"- {item['content']}" for item in items
    )


async def delete_important_memory(keyword: str) -> int:
    """按关键词模糊匹配删除重要记忆，返回删除条数"""
    global _important_memories_text
    existing = await _load_important_items()
    new_items = [m for m in existing if keyword not in m.get("content", "")]
    deleted = len(existing) - len(new_items)
    if deleted > 0:
        await _save_important_items(new_items)
        await refresh_important_cache()
        logger.info(f"重要记忆已删除 {deleted} 条（关键词: {keyword}）")
    return deleted


async def save_important_memory(memory_text: str, check_dup=None) -> dict:
    """
    保存重要记忆。check_dup 为 async (items, new_text) -> bool 的去重回调。
    返回 {"saved": bool, "duplicate": bool}。
    """
    global _important_memories_text
    existing = await _load_important_items()

    # 小模型去重
    if check_dup and existing:
        try:
            is_dup = await check_dup(existing, memory_text)
            if is_dup:
                logger.info(f"重要记忆去重跳过: {memory_text[:40]}")
                return {"saved": False, "duplicate": True}
        except Exception as e:
            logger.warning(f"去重检查失败，继续保存: {e}")

    existing.append({"timestamp": get_time(), "content": memory_text})
    await _save_important_items(existing)
    await refresh_important_cache()
    logger.info(f"重要记忆已保存: {memory_text}")
    return {"saved": True, "duplicate": False}


# ========== 历史记录操作 ==========
async def add_user_message(content: str) -> None:
    async with _lock:
        history.append({"role": "user", "content": content})
        await save_history()


async def add_assistant_record(msg: dict) -> None:
    async with _lock:
        history.append(msg)
        await save_history()


async def add_system_note(content: str) -> None:
    if not content:
        return
    async with _lock:
        history.append({"role": "system", "content": content})
        await save_history()


async def add_records(records: list[dict]) -> None:
    if not records:
        return
    async with _lock:
        history.extend(records)
        await save_history()


async def get_history_length() -> int:
    async with _lock:
        return len(history)


async def get_history_slice(end: int) -> list[dict]:
    """获取历史记录的前 end 条（线程安全）。"""
    async with _lock:
        return list(history[:end])


async def truncate_history(cut_point: int, new_important: list[dict]) -> None:
    global history, _important_memories_text
    async with _lock:
        history[:] = history[cut_point:]
        await save_history()
        await _save_important_items(new_important)
    await refresh_important_cache()
    logger.info(f"历史截断于 {cut_point}，保留 {len(history)} 条")


def _build_admin_info() -> str:
    """从 PERSONA_VARS 构建管理员信息文本"""
    admins = PERSONA_VARS.get("admins", [])
    if not admins:
        return ""
    lines = ["你的管理员："]
    for a in admins:
        name = a.get("name", "未知")
        qq = a.get("qq", "")
        role_note = f"（{a.get('role')}）" if a.get("role") else ""
        lines.append(f"- {name}（QQ {qq}）{role_note}")
    return "\n".join(lines)


# ========== 四区组装 ==========
def build_messages(current_context: str = "", system_override: str | None = None) -> list[dict]:
    """
    Zone 1: 系统提示词（永不变 → 缓存命中）
    Zone 2: 对话历史（仅追加 → 前缀缓存）
    Zone 3: 重要记忆（体量小，变化时代价低）
    Zone 4: 当前上下文（每次不同）

    system_override: 如果不为 None，用它替代默认的 COMBINED_SYSTEM。
    """
    system = system_override if system_override is not None else COMBINED_SYSTEM
    messages = [{"role": "system", "content": system}]

    admin_info = _build_admin_info()
    if admin_info:
        messages.append({"role": "system", "content": admin_info})

    messages.extend(history)
    if _important_memories_text:
        messages.append({"role": "system", "content": _important_memories_text})
    if current_context:
        messages.append({"role": "system", "content": current_context})
    return messages


# ========== 测试模式清理 ==========
def _cleanup_test_files() -> None:
    """退出时删除测试模式的临时记忆文件"""
    for f in (MEMORY_FILE, IMPORTANT_FILE):
        if os.path.exists(f):
            try:
                os.remove(f)
                logger.info(f"测试模式：已删除临时文件 {f}")
            except OSError as e:
                logger.warning(f"测试模式：删除临时文件失败 {f}: {e}")


# ========== 初始化 ==========
async def init_memory() -> None:
    global history, _important_memories_text

    # 确保人格目录存在
    os.makedirs(_PERSONA_DIR, exist_ok=True)

    if _TEST_MODE:
        logger.info("测试模式：不加载已有记忆，使用临时文件")
        history = []
        _important_memories_text = ""
        atexit.register(_cleanup_test_files)
    else:
        history = await load_history()
        await refresh_important_cache()
        logger.info(f"[{_PERSONA_NAME}] 已加载 {len(history)} 条对话历史")
        if _important_memories_text:
            logger.info("已加载重要记忆")
