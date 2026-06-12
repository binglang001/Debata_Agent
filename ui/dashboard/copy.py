"""仪表盘文案 —— 所有用户可见的中文字符串。

命名规则同 wizard/copy.py：
    {area}.{element}.{state}
"""

from __future__ import annotations

DASHBOARD_COPY: dict[str, str] = {
    # ============================================================
    # 窗口标题
    # ============================================================
    "window.title": "Debata_Agent",

    # ============================================================
    # 导航
    # ============================================================
    "nav.overview": "总览",
    "nav.chats": "对话",
    "nav.memory": "记忆",
    "nav.logs": "日志",
    "nav.personas": "角色",
    "nav.settings": "设置",
    "nav.models": "模型管理",

    # ============================================================
    # 顶部状态栏
    # ============================================================
    "topbar.adapter_connected": "已就位",
    "topbar.adapter_connecting": "连接中",
    "topbar.adapter_disconnected": "未连接",
    "topbar.adapter_error": "出错了",
    "topbar.persona_label": "当前角色：",
    "topbar.usage_label": "今日用量",
    "topbar.theme_toggle": "切换深浅",

    # ============================================================
    # 总览面板
    # ============================================================
    "overview.adapter_title": "渠道状态",
    "overview.adapter_address_label": "地址",
    "overview.adapter_uptime_label": "已连接",
    "overview.adapter_reconnect": "重连",

    "overview.providers_title": "模型健康",
    "overview.provider_status_ok": "可用",
    "overview.provider_status_unknown": "未检测",
    "overview.provider_status_warning": "响应慢",
    "overview.provider_status_error": "无响应",
    "overview.provider_latency_label": "近 5 分钟平均延迟",

    "overview.stats_title": "累计概况",
    "overview.stats_messages_in": "收到消息",
    "overview.stats_messages_out": "发出消息",
    "overview.stats_tool_calls": "工具调用",
    "overview.stats_tokens_in": "输入 token",
    "overview.stats_tokens_out": "输出 token",
    "overview.stats_cache_hit": "缓存命中率",
    "overview.stats_estimated_cost": "估算成本",

    # ============================================================
    # 对话可视化
    # ============================================================
    "chats.empty_title": "暂无对话",
    "chats.empty_subtitle": "机器人就位后，往来的消息会显示在这里",
    "chats.list_title": "对话列表",
    "chats.list_filter_all": "全部",
    "chats.list_filter_private": "私聊",
    "chats.list_filter_group": "群聊",
    "chats.list_filter_active": "活跃中",
    "chats.detail_thinking": "正在思考",
    "chats.detail_typing": "正在回复",
    "chats.detail_idle": "空闲",
    "chats.detail_tool_call": "调用了 ",
    "chats.detail_reasoning": "思考过程",
    "chats.detail_expand_reasoning": "展开思考",
    "chats.detail_collapse_reasoning": "收起",

    # ============================================================
    # 记忆面板
    # ============================================================
    "memory.empty_title": "尚未记下什么",
    "memory.empty_subtitle": "对话中若有值得记住的事，会自动记录在此",
    "memory.important_section": "重要记忆",
    "memory.recent_section": "最近对话",
    "memory.edit_button": "编辑",
    "memory.delete_button": "移除",
    "memory.delete_confirm_title": "移除这条记忆？",
    "memory.delete_confirm_body": "Debata 之后不会再用它做参考。这是不可逆的操作。",
    "memory.export_button": "导出",
    "memory.import_button": "导入",

    # ============================================================
    # 日志查看器
    # ============================================================
    "logs.empty_title": "日志为空",
    "logs.filter_level": "等级",
    "logs.filter_module": "模块",
    "logs.filter_time": "时间范围",
    "logs.search_placeholder": "搜索日志……",
    "logs.export_button": "导出",
    "logs.clear_button": "清空显示",
    "logs.level_debug": "调试",
    "logs.level_info": "信息",
    "logs.level_warning": "留意",
    "logs.level_error": "出错",

    # ============================================================
    # 设置面板
    # ============================================================
    "settings.section_model": "模型",
    "settings.section_features": "功能",
    "settings.section_adapter": "渠道",
    "settings.section_persona": "角色",
    "settings.section_appearance": "外观",
    "settings.section_advanced": "高级",
    "settings.unsaved_changes": "有未保存的修改",
    "settings.save_button": "记住",
    "settings.discard_button": "丢弃修改",
    "settings.restart_required": "改动需要重启 Debata 才能生效",
    "settings.restart_button": "立即重启",

    "settings.appearance_theme": "主题",
    "settings.appearance_theme_auto": "跟随系统",
    "settings.appearance_theme_light": "浅色",
    "settings.appearance_theme_dark": "深色",
    "settings.appearance_font_size": "字号",

    # ============================================================
    # 角色管理
    # ============================================================
    "personas.list_title": "角色列表",
    "personas.add_button": "AI 生成角色",
    "personas.activate_button": "切换为当前",
    "personas.duplicate_button": "复制一份",
    "personas.delete_button": "删除",
    "personas.import_button": "从文件导入",
    "personas.export_button": "导出为文件",
    "personas.builtin_badge": "内置",
    "personas.active_badge": "当前",
    "personas.delete_confirm_title": "删除这个角色？",
    "personas.delete_confirm_body": (
        "角色定义会被移除，但已有的对话历史会保留。\n"
        "如果当前激活的就是它，需要在删除后选一个新角色。"
    ),
    "personas.delete_active_warning": "这是当前角色，请先切换到其它角色再删除。",

    # ============================================================
    # 模型管理
    # ============================================================
    "models.empty_title": "暂无本地模型",
    "models.empty_subtitle": "插件目录下未发现本地模型。",
    "models.download_button": "安装指引",
    "models.open_dir_button": "打开目录",
    "models.rescan_button": "重新扫描",

    # ============================================================
    # 系统托盘
    # ============================================================
    "tray.menu_dashboard": "打开仪表盘",
    "tray.menu_pause": "暂停响应",
    "tray.menu_resume": "继续响应",
    "tray.menu_restart": "重启 Debata",
    "tray.menu_quit": "退出",
    "tray.notify_connected": "Debata 已就位",
    "tray.notify_disconnected": "Debata 断线了",
    "tray.notify_error_title": "出了点状况",

    # ============================================================
    # 通用按钮
    # ============================================================
    "button.confirm": "就这样",
    "button.cancel": "算了",
    "button.refresh": "刷新",
    "button.copy": "复制",
    "button.copy_success": "已复制",

    # ============================================================
    # 启动 / 重启状态
    # ============================================================
    "splash.starting": "正在启动",
    "splash.loading_config": "读取配置",
    "splash.loading_secrets": "解密密钥",
    "splash.loading_persona": "唤起角色",
    "splash.loading_history": "翻看历史",
    "splash.connecting_adapter": "接通渠道",
    "splash.ready": "一切就位",
}
