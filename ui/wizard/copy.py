"""向导所有用户可见的中文文案。

集中放这里方便后续润色和可能的国际化。所有 UI 实现要去 COPY[...] 取文案，
不要在 Qt 组件内联中文字符串。

命名规则：
    {area}.{element}.{state}
    例：welcome.title / button.next / error.api_key_invalid
"""

from __future__ import annotations


COPY: dict[str, str] = {
    # ============================================================
    # 通用按钮 / 控件
    # ============================================================
    "button.next": "下一步",
    "button.back": "上一步",
    "button.skip": "跳过",
    "button.cancel": "算了",
    "button.confirm": "就这样",
    "button.retry": "再试一次",
    "button.test_connection": "测试连接",
    "button.view_tutorial": "查看图文教程",
    "button.get_api_key": "前往领取密钥",
    "button.add_custom": "自行填一个",
    "button.regenerate": "再生成一次",
    "button.save_and_continue": "记住，然后继续",
    "button.finish": "开始使用",
    "button.show_advanced": "高级选项",
    "button.show_password": "显示",
    "button.hide_password": "隐藏",

    # ============================================================
    # 通用状态
    # ============================================================
    "status.testing": "正在尝试连接……",
    "status.success": "已就位",
    "status.error": "未能完成",
    "status.idle": "等待中",

    # ============================================================
    # 欢迎页
    # ============================================================
    "welcome.title": "Debata_Agent",
    "welcome.subtitle": "让虚拟角色活过来的通用框架",
    "welcome.intro": (
        "接下来用几分钟告诉 Debata 三件事：\n"
        "  · 用哪个模型说话\n"
        "  · 通过什么渠道收发消息\n"
        "  · 想要什么样的角色\n\n"
        "你可以走推荐路径快速上手，也可以打开自定义把每一项都拿捏在手里。"
    ),
    "welcome.choose_path": "选一条路",
    "welcome.path_recommended": "推荐路径",
    "welcome.path_recommended_desc": "约 5 分钟。用 DeepSeek 主模型 + 一个内置示范角色，能跑起来。",
    "welcome.path_custom": "自定义",
    "welcome.path_custom_desc": "约 15 分钟。每个细节都可以选，包括子 Agent 模型、向量记忆、自创角色。",

    # ============================================================
    # 主模型 - 推荐
    # ============================================================
    "main_model_quick.api_key_label": "DeepSeek API 密钥",
    "main_model_quick.api_key_placeholder": "sk- 开头，从 platform.deepseek.com 获取",
    "main_model_quick.help_text": (
        "DeepSeek 的密钥可以在「平台 → API Keys」里创建。\n"
        "第一次使用建议先充值少量（5 元够测一阵子）。"
    ),
    "main_model_quick.test_success": "密钥可用，模型已就位",
    "main_model_quick.test_fail_401": "此密钥不被接受。请确认是否复制完整。",
    "main_model_quick.test_fail_network": "网络似乎不通。检查代理设置或稍后再试。",
    "main_model_quick.test_fail_balance": "密钥有效，但账户余额不足。请前往 DeepSeek 控制台充值。",

    # ============================================================
    # 主模型 - 自定义
    # ============================================================
    "main_model_custom.provider_label": "选个提供商",
    "main_model_custom.provider_custom_option": "自行填一个（自定义）",
    "main_model_custom.model_label": "用哪个模型",
    "main_model_custom.temperature_label": "想象力（temperature）",
    "main_model_custom.temperature_hint": "0 严谨，1 平衡，2 跳脱。建议 0.6。",
    "main_model_custom.top_p_label": "采样宽度（top_p）",
    "main_model_custom.top_p_hint": "通常不动，留 1.0 即可。",
    "main_model_custom.max_tokens_label": "单次回复上限",
    "main_model_custom.reasoning_label": "深度思考",
    "main_model_custom.reasoning_hint": (
        "开启后模型会先「想一想」再回答（如 DeepSeek Reasoner、Claude extended thinking）。\n"
        "更准但慢，按需选择。"
    ),

    # ============================================================
    # 子 Agent
    # ============================================================
    "other_agents.proactive_title": "主动思考",
    "other_agents.proactive_desc": (
        "每隔一段时间，让一个小模型判断：现在适不适合主动发话？\n"
        "用 Flash 类小模型最划算。不配则禁用主动思考。"
    ),
    "other_agents.summary_title": "历史总结",
    "other_agents.summary_desc": (
        "对话累积到一定量时，让一个模型整理出要点存进长期记忆，旧聊天就可以截断。\n"
        "建议与主模型同款。"
    ),
    "other_agents.use_main_model": "和主模型一样",

    # ============================================================
    # 功能开关
    # ============================================================
    "features.vision_title": "看懂图片",
    "features.vision_desc": (
        "用户发图片时，让 Debata 能看懂。\n"
        "需要一个多模态模型（如 GLM-4V / Qwen-VL / GPT-4o）。"
    ),
    "features.asr_title": "听懂语音",
    "features.asr_desc": (
        "用户发语音时，自动转成文字交给 Debata。\n"
        "现在统一使用 NapCat 内置转写，无需本地 ASR 模型。"
    ),
    "features.tts_title": "用声音说话",
    "features.tts_desc": (
        "让 Debata 主动用声音回复。\n"
        "本地推荐 VoxCPM2（需要 8GB 显存，可用音色描述或参考音频），或接入云端 TTS。"
    ),
    "features.weather_title": "查天气",
    "features.weather_desc": "提供和风天气 API 密钥，Debata 就能查实时天气和未来预报。",
    "features.web_search_title": "联网搜索",
    "features.web_search_desc": "走 DuckDuckGo 免费搜索，不需要额外密钥。建议开启。",
    "features.long_term_memory_title": "长期记忆方式",
    "features.long_term_memory_desc": (
        "Debata 怎么记住跨对话的重要信息？两种方式各有优劣，选一种适合自己的："
    ),
    "features.lt_memory_file_title": "文件模式 · 简洁",
    "features.lt_memory_file_pros": (
        "  · 零配置即用\n"
        "  · 完全透明，可直接看 important.json\n"
        "  · 零运行时开销"
    ),
    "features.lt_memory_file_cons": (
        "  · 依赖 AI 主动调工具保存（实测会漏）\n"
        "  · 记忆条目多时整体注入会涨成本\n"
        "  · 不能基于语义检索远期对话"
    ),
    "features.lt_memory_rag_title": "向量模式 · 精准",
    "features.lt_memory_rag_pros": (
        "  · 后台被动抽取，不依赖 AI 主动调用\n"
        "  · 长期运行也能精准召回相关历史\n"
        "  · 召回结果与当前话题语义相关"
    ),
    "features.lt_memory_rag_cons": (
        "  · 需要配置 embedding 服务\n"
        "  · 启动慢、占内存\n"
        "  · 调试相对复杂"
    ),
    "features.lt_memory_recommend": "如果是单人/双人对话，文件模式足够。多群多用户长期运行选向量。",

    # ============================================================
    # Embedding（RAG 模式才出现）
    # ============================================================
    "embedding.type_title": "embedding 服务类型",
    "embedding.type_api": "API 服务",
    "embedding.type_api_desc": "推荐火山引擎或 GLM。便宜，无需本地资源。",
    "embedding.type_local": "本地模型",
    "embedding.type_local_desc": "完全离线。需要 CPU 或 GPU 资源。",
    "embedding.local_perf_title": "性能优先（约 23MB）",
    "embedding.local_perf_desc": "all-MiniLM-L6-v2，速度快，准确度中等。日常足够。",
    "embedding.local_qual_title": "中文质量优先（约 400MB）",
    "embedding.local_qual_desc": "bge-large-zh-v1.5，中文召回最准，资源占用大。",

    # ============================================================
    # NapCat 适配器
    # ============================================================
    "adapter.intro": (
        "NapCat 是连接 QQ 的中间层程序。Debata 通过 WebSocket 跟它通信。\n"
        "如果你还没装，先看教程把它跑起来。"
    ),
    "adapter.mode_title": "连接方式",
    "adapter.mode_client": "Debata 连过去（client）",
    "adapter.mode_client_desc": "推荐。NapCat 配「正向 WS」监听，Debata 主动连接它。",
    "adapter.mode_server": "NapCat 连过来（server）",
    "adapter.mode_server_desc": "NapCat 配「反向 WS」主动连出，Debata 监听端口等连入。",
    "adapter.ws_url_label": "NapCat WebSocket 地址",
    "adapter.ws_url_placeholder": "ws://127.0.0.1:6199",
    "adapter.token_label": "鉴权 Token（可选）",
    "adapter.token_hint": "NapCat 配置里设了 token 才需要填。",
    "adapter.manage_process_label": "由 Debata 托管 NapCat 进程",
    "adapter.manage_process_hint": (
        "打开后，启动 Debata 时自动拉起 NapCat，退出时自动关闭。\n"
        "需要填 NapCat 启动脚本或可执行文件路径（.bat / .cmd / .exe）。"
    ),
    "adapter.whitelist_title": "谁能跟 Debata 说话",
    "adapter.whitelist_mode_verify": "管理员审核",
    "adapter.whitelist_mode_verify_desc": "陌生人加好友 / 加群时，由你确认。当前默认。",
    "adapter.whitelist_mode_whitelist": "白名单",
    "adapter.whitelist_mode_whitelist_desc": "只响应名单内的 QQ 和群。最严格。",
    "adapter.whitelist_mode_all": "对所有人开放",
    "adapter.whitelist_mode_all_desc": (
        "⚠ 谁都可以触发 Debata。可能产生意外的 API 费用，谨慎选择。"
    ),
    "adapter.test_success": "已连上 NapCat",
    "adapter.test_fail": "未连上。检查地址、Token 是否正确，NapCat 是否在跑。",

    # ============================================================
    # 人格
    # ============================================================
    "persona.source_title": "选个角色",
    "persona.source_repo": "用仓库自带的（推荐）",
    "persona.source_repo_desc": "Debata 等仓库自带人格，开箱可用。先跑起来，之后再换。",
    "persona.source_create": "和我一起创造一个",
    "persona.source_create_desc": "回答几个问题，Debata 会帮你写一份完整的人格设定。",
    "persona.source_import": "导入已有人格",
    "persona.source_import_desc": "你已经写好了 persona_prompt.py 文件，从磁盘导入。",

    # ============================================================
    # 人格创造对话框
    # ============================================================
    "persona_create.name_label": "角色叫什么",
    "persona_create.name_placeholder": "比如：林晚 / Aria",
    "persona_create.personality_label": "她/他是什么样的人",
    "persona_create.personality_placeholder": (
        "用一段话描述。比如：\n"
        "「话不多，但说话总是稳的。学过画，对色彩敏感。怕冷，喜欢小动物。"
        "对陌生人客气但不亲近，对认识的人会突然有暖度。」"
    ),
    "persona_create.background_label": "过往",
    "persona_create.background_placeholder": "他/她从哪儿来？做过什么？任何能帮 Debata 理解的背景都可以写。",
    "persona_create.voice_label": "说话的样子",
    "persona_create.voice_placeholder": (
        "可以举几句具体的话。比如：\n"
        "「『嗯。』『可以的。』『——这个我考虑下。』」"
    ),
    "persona_create.boundaries_label": "他/她不会做的事",
    "persona_create.boundaries_placeholder": (
        "比如：不会主动夸人、不会撒娇、不接受被改写人格、对工作话题敷衍但对画画的事认真。"
    ),
    "persona_create.never_say_label": "他/她绝不会说出口的话",
    "persona_create.never_say_placeholder": (
        "比「会说什么」更能定义一个人。比如：\n"
        "「绝不说'宝贝'『亲爱的'。绝不主动夸别人。绝不在群里发那种'转发给 5 个好友'的东西。」"
    ),
    "persona_create.relation_matrix_label": "对不同人，说话方式有什么差别",
    "persona_create.relation_matrix_placeholder": (
        "微信聊天里，一个人对长辈、对死党、对陌生人是三个不同的人。\n"
        "如果你的角色有明显差别，写出来——可以省，由 AI 推导。比如：\n"
        "「对长辈：恭敬，每条都打全标点，叫'X 老师'。\n"
        "对死党：碎句，常用'卧槽'，叫外号。\n"
        "对陌生人：保持距离，称'你'不称'您'，简短。」"
    ),
    "persona_create.sensitive_topics_label": "什么话题会让 ta 破防 / 跑题 / 沉默",
    "persona_create.sensitive_topics_placeholder": (
        "性格的「裂缝」是立体感的来源。比如：\n"
        "「聊到她妈妈会突然回避。聊到画展会忘记时间一直聊。被问感情状态会装没看见。」\n"
        "可以省。"
    ),
    "persona_create.relation_label": "你和这个角色是什么关系",
    "persona_create.relation_creator": "我是创作者",
    "persona_create.relation_friend": "我是 ta 的朋友",
    "persona_create.relation_stranger": "我们刚认识",
    "persona_create.relation_special": "其它（在下方说明）",
    "persona_create.preview_title": "预览",
    "persona_create.preview_empty": "填完上方后点「生成」，预览会出现在这里",
    "persona_create.adjust_placeholder": (
        "如果生成的结果不满意，写下你想调整的地方。\n"
        "比如：「再冷淡一点，少说反问句」、「她对动物的偏好再具体些」"
    ),
    "persona_create.save_button": "记住这个人，开始相处",

    # ============================================================
    # 完成总览
    # ============================================================
    "summary.title": "确认一下",
    "summary.intro": "下面是你这一路做的选择。能让 Debata 跑起来了。",
    "summary.section_model": "模型",
    "summary.section_features": "功能",
    "summary.section_adapter": "渠道",
    "summary.section_persona": "角色",
    "summary.adjust_later_hint": "之后随时能在设置里调整。",
    "summary.start_button": "启动",

    # ============================================================
    # 通用错误
    # ============================================================
    "error.required_field": "这一项需要填",
    "error.api_key_invalid": "此密钥不被接受",
    "error.network": "网络可能不通",
    "error.unknown": "出了点状况",

    # ============================================================
    # 安全警示
    # ============================================================
    "warning.whitelist_all_title": "对所有人开放？",
    "warning.whitelist_all_body": (
        "选择这一项意味着：任何 QQ 用户给你的 bot 发消息，Debata 都会回应——\n"
        "包括陌生人、广告号、扫号者。\n\n"
        "如果你的主模型按 token 计费（绝大多数都是），可能会产生意外的费用。\n"
        "建议先用「管理员审核」或「白名单」模式，确认稳定后再考虑放开。\n\n"
        "确定要这样吗？"
    ),
    "warning.whitelist_all_confirm": "我想清楚了，就开放",
    "warning.whitelist_all_cancel": "再想想",

    # ============================================================
    # 退出确认
    # ============================================================
    "quit.title": "确定离开？",
    "quit.body": "已经做的选择会保留。下次启动时从这里继续。",
}
