# Version note: queue-ordered replies, non-text safety, preserve user context on AI failure
import time
import threading
import datetime
import signal
import sys
import telebot
from requests.exceptions import ConnectTimeout
from config import Config
from database import Database
from utils import (
    AsyncLogger, ChatLockManager, ContextCacheManager, OnetimeCodeManager, BotHelper, ProviderManager
)

from services import (
    _normalize_super_admin_ids,
    AuthManager,
    SettingsManager,
    RateLimiter,
    UsageManager,
    ChatQueueManager,
    check_and_prepare_task,
    apply_summary_success,
    _get_context_slice_for_reply,
    _insert_ai_reply,
    _build_effective_system_prompt
)

from handlers import register_handlers

cfg = Config()

cfg.validate()

db = Database(cfg.DB_FILE)

import traceback
def _log_exception(context, exc):
    print(f"[Error] {context}: {exc}")
    traceback.print_exc()
    try:
        db.add_system_log("error", source=context, detail=f"{type(exc).__name__}: {exc}")
    except Exception:
        pass

_last_polling_err = {"ts": 0}
def _log_polling_error_brief(e, cooldown=60):
    now = time.time()
    if now - _last_polling_err["ts"] >= cooldown:
        _last_polling_err["ts"] = now
        print(f"[Warn] Telegram polling: {type(e).__name__}: {e}")

_shutdown_event = threading.Event()
_shutdown_executed = threading.Lock()
_shutdown_done = False

async_logger = AsyncLogger(db, _shutdown_event)
chat_locks = ChatLockManager(_shutdown_event)
context_manager = ContextCacheManager(db, _shutdown_event)
onetime_code_manager = OnetimeCodeManager(db)

bot = telebot.TeleBot(cfg.TG_BOT_TOKEN)
bot_helper = BotHelper(bot, cfg)

provider_manager = ProviderManager(cfg, db)

auth_manager = AuthManager(db, _normalize_super_admin_ids(cfg.SUPER_ADMIN_ID))
auth_manager.sync_super_admins()

settings_manager = SettingsManager(db)

rate_limiter = RateLimiter(db, auth_manager, cfg)
usage_manager = UsageManager(db, auth_manager, cfg)

chat_queue_manager = ChatQueueManager(_shutdown_event, _log_exception)

# 注册 handlers
register_handlers(
    bot=bot,
    cfg=cfg,
    auth_manager=auth_manager,
    settings_manager=settings_manager,
    rate_limiter=rate_limiter,
    usage_manager=usage_manager,
    context_manager=context_manager,
    chat_locks=chat_locks,
    async_logger=async_logger,
    onetime_code_manager=onetime_code_manager,
    provider_manager=provider_manager,
    bot_helper=bot_helper,
    chat_queue_manager=chat_queue_manager,
    log_exception=_log_exception,
    check_and_prepare_task=check_and_prepare_task,
    apply_summary_success=apply_summary_success,
    build_effective_system_prompt=_build_effective_system_prompt,
    get_context_slice_for_reply=_get_context_slice_for_reply,
    insert_ai_reply=_insert_ai_reply,
)

def _do_shutdown():
    global _shutdown_done
    with _shutdown_executed:
        if _shutdown_done:
            return
        _shutdown_done = True
    print("\n[System] 正在执行清理程序...")
    _shutdown_event.set()
    print(" -> 正在保存所有对话上下文...")
    context_manager.flush_all()
    print(" -> 正在停止异步日志记录器...")
    async_logger.stop()
    print(" -> 正在关闭数据库连接...")
    db.close()
    print("[System] ✅ 所有资源已释放，程序已完全退出。")

def _signal_handler(signum, frame):
    print(f"\n[System] 🛑 接收到终止信号 ({signum})，准备停止...")
    _shutdown_event.set()

signal.signal(signal.SIGINT, _signal_handler)
if sys.platform != 'win32':
    signal.signal(signal.SIGTERM, _signal_handler)

def bot_polling_worker():
    try:
        bot.remove_webhook()
    except Exception as e:
        _log_polling_error_brief(e)

    backoff = 1
    while not _shutdown_event.is_set():
        try:
            bot.polling(non_stop=False, interval=1, timeout=20)
            backoff = 1
        except ConnectTimeout as e:
            _log_polling_error_brief(e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except Exception as e:
            if _shutdown_event.is_set():
                break
            _log_polling_error_brief(e)
            time.sleep(3)

def main():
    print(f"""
╔══════════════════════════════════════════╗
║  🤖 Telegram AI助手 v{cfg.VERSION}            
║  构建日期: {cfg.BUILD_DATE}                   
║  功能: {cfg.DESCRIPTION}
║  限制: {cfg.USER_RATE_LIMIT_HOURLY}/h, {cfg.USER_RATE_LIMIT_DAILY}/d
╚══════════════════════════════════════════╝
""")

    try:
        polling_thread = threading.Thread(target=bot_polling_worker, name="BotPoller", daemon=True)
        polling_thread.start()

        print("[System] 🚀 服务已启动，正在监听消息...")
        last_report_time = 0
        report_interval = 3600
        while not _shutdown_event.is_set():
            current_time = time.time()
            if current_time - last_report_time > report_interval:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                active_threads = threading.active_count()
                print(f"[Status {ts}] 🟢 运行中 | 活跃线程数: {active_threads}")
                last_report_time = current_time
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[System] 检测到键盘中断 (Ctrl+C)...")
        _shutdown_event.set()
    finally:
        try:
            print("[System] 正在停止 Bot 轮询...")
            bot.stop_polling()
        except Exception:
            pass
        _do_shutdown()

if __name__ == "__main__":
    main()
