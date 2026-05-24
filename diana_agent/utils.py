from datetime import datetime


def get_time() -> str:
    """返回当前时间的格式化字符串，用于注入 AI 上下文"""
    now = datetime.now()
    return now.strftime("%Y年%m月%d日 %A %H:%M:%S")
