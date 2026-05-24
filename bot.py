#!/usr/bin/env python3
"""Diana QQ 机器人入口 —— 基于 NoneBot2 + OneBot V11"""

import os
import sys

# --test 测试模式：使用临时记忆文件，退出后自动删除，不影响正式记忆
if "--test" in sys.argv:
    os.environ["DIANA_TEST_MODE"] = "true"
    sys.argv.remove("--test")

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

# 初始化 NoneBot（从 .env 读取配置）
nonebot.init()

# 注册 OneBot V11 适配器
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 加载 Diana-agent 插件
nonebot.load_plugin("diana_agent")

if __name__ == "__main__":
    nonebot.run()
