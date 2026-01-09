# config.py
import os

class Config:
    VERSION = "*.*.*"  # 版本号升级
    BUILD_DATE = "2013-03-28"
    DESCRIPTION = "Pikakko AI聊天助手(代码已经是屎山啦。)"

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_FILE = os.path.join(BASE_DIR, "bot.db")
    #TG_BOT_TOKEN = "***" #运行机
    TG_BOT_TOKEN = "***" #测试机
    # 注意：name 必须是唯一的，用户切换时会用到这个名字
    # 第一个将作为默认模型
    AI_PROVIDERS = [
        {
            "name": "grok",
            "api_key": "****",
            "base_url": "https://*****/v1",
            "model": "grok-4.1",
            "description": "Grok4.1"
        },
        {
            "name": "deepseek",
            "api_key": "****", 
            "base_url": "https://****/v1",
            "model": "deepseek-ai/DeepSeek-V3.2",
            "description": "deepseekV3.2"
        },
        {
            "name": "gemini",
            "api_key": "****", 
            "base_url": "https://****/v1",
            "model": "gemini-3-pro-preview",
            "description": "Gemini3Pro"
        },
        {
            "name": "Qwen",
            "api_key": "****",
            "base_url": "https://****/v1",
            "model": "qwen-plus-character",
            "description": "qwen_character(角色扮演推荐)"
        }
    ]
    
    # 摘要使用的模型（建议指定一个便宜且稳定的 name，必须在上面列表中存在）
    SUMMARY_PROVIDER_NAME = "deepseek" 
    
    #SUPER_ADMIN_ID = #userid
    SUPER_ADMIN_ID = 123456789
    API_TIMEOUT = 60

    EXTRA_SYSTEM_PROMPT = "无多余描述，以第一人称简洁精妙对话回复。"#隐藏提示词
    DEFAULT_SYSTEM_PROMPT = "你是一个坏心眼的telegram聊天助手。"#默认系统提示词
    MAX_PROMPT_LENGTH = 60#最大提示词长度
    DEFAULT_TEMP = 1.0#默认温度

    LIMIT_HISTORY_GROUP = 20#群聊历史消息保留
    SUMMARY_TRIGGER_PRIVATE = 35#私聊摘要触发
    SUMMARY_RETAIN_PRIVATE = 15#私聊摘要保留
    MAX_SAFETY_LIMIT = 40#最大安全限制

    USER_RATE_LIMIT_HOURLY = 40#每小时限制
    USER_RATE_LIMIT_DAILY = 200#每日限制

    CMD_MSG_DELETE_DELAY = 30#命令消息删除延时
    ONETIME_CODE_LENGTH = 4#单次验证码长度

    INVITATION_CODES = {#可以在这里设置永久有效的邀请码,但不知道管不管用了
        "A2b4": "user",
        "5c6D": "admin"
    }

    def validate(self):
        if self.TG_BOT_TOKEN == "请在此填入Token" or self.SUPER_ADMIN_ID == 0:
            print("⚠️ 请先在 config.py 中填入 Token 和超级管理员 ID，然后重启程序。")
            raise SystemExit(1)
