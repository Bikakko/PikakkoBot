# services.py
import time
import threading
import uuid
import datetime
import queue


def _normalize_super_admin_ids(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [r.strip() for r in raw.split(",") if r.strip()]
    elif not isinstance(raw, (list, tuple, set)):
        raw = [raw]

    ids = []
    for v in raw:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(set(ids))


class AuthManager:
    def __init__(self, db, super_admin_ids):
        self.db = db
        self._lock = threading.RLock()
        self.super_admin_ids = set(super_admin_ids or [])

    def sync_super_admins(self):
        with self._lock:
            desired = set(self.super_admin_ids)
            existing = set(self.db.list_super_admin_ids())

            for uid in existing - desired:
                self.db.delete_user(uid)
                self.db.add_system_log("移除超级管理员", uid, str(uid), "config-sync")

            for uid in desired:
                self.db.upsert_user(uid, "super_admin", "超级管理员")
                self.db.add_system_log("添加超级管理员", uid, str(uid), "config-sync")

    def get_role(self, user_id):
        if user_id in self.super_admin_ids:
            return "super_admin"
        row = self.db.get_user(user_id)
        return row["role"] if row else None

    def is_super_admin(self, user_id):
        return user_id in self.super_admin_ids

    def is_admin(self, user_id):
        return self.get_role(user_id) in ("admin", "super_admin")

    def is_whitelisted(self, user_id):
        return self.get_role(user_id) in ("user", "admin", "super_admin")
        
    def can_use_chat(self, user_id, chat_type):
        # 管理员永远允许
        if self.is_admin(user_id):
            return True
        # 群聊和私聊都要求在白名单内
        return self.is_whitelisted(user_id)

    def add_admin_by_invite(self, target_id, source="invite", user_obj=None):
        # 邀请码路径授权：admin 角色直接写入，不做“超级管理员”限制
        with self._lock:
            display_name = self.get_display_name(user_obj) if user_obj else None
            self.db.upsert_user(target_id, "admin", display_name)
            name = display_name if display_name else str(target_id)
            # 记录日志，方便追踪邀请码来源
            self.db.add_system_log("邀请码添加管理员", target_id, name, source)

    def should_rate_limit(self, user_id, chat_type):
        if self.is_admin(user_id):
            return False
        if chat_type != "private" and self.get_role(user_id) == "user":
            return True
        return True


    def get_display_name(self, user_obj):
        first = user_obj.first_name or ""
        last = user_obj.last_name or ""
        username = f"(@{user_obj.username})" if user_obj.username else ""
        return f"{first} {last} {username}".strip() or "Unknown"

    def update_user_info(self, user_id, display_name):
        threading.Thread(target=self.db.update_display_name, args=(user_id, display_name), daemon=True).start()

    def add_admin(self, target_id, operator_id, source="admin", user_obj=None):
        with self._lock:
            if not self.is_super_admin(operator_id):
                self.db.add_system_log(
                    "拒绝添加管理员",
                    target_id,
                    str(target_id),
                    f"{source}:operator={operator_id}"
                )
                raise PermissionError("仅超级管理员可添加管理员")
            self.db.upsert_user(target_id, "admin", self.get_display_name(user_obj) if user_obj else None)
            name = self.get_display_name(user_obj) if user_obj else str(target_id)
            self.db.add_system_log("添加管理员", target_id, name, source)

    def add_user(self, target_id, source="admin", user_obj=None):
        with self._lock:
            self.db.upsert_user(target_id, "user", self.get_display_name(user_obj) if user_obj else None)
            name = self.get_display_name(user_obj) if user_obj else str(target_id)
            self.db.add_system_log("添加白名单", target_id, name, source)

    def del_user(self, target_id, operator_id, source="admin"):
        """
        移除用户或管理员。
        operator_id: 发起删除操作的人的ID
        """
        with self._lock:
            # 1. 获取目标的角色
            target_role = self.get_role(target_id)
            
            # 2. 如果目标不存在，直接返回或忽略
            if not target_role:
                return

            # 3. 权限检查逻辑
            if self.is_super_admin(operator_id):
                # 超级管理员可以删除任何人（除了自己，建议加个防手滑校验，可选）
                pass 
            elif self.is_admin(operator_id):
                # 普通管理员尝试删除人
                if target_role in ("super_admin", "admin"):
                    self.db.add_system_log(
                        "拒绝移除用户", target_id, str(target_id), 
                        f"{source}:operator={operator_id}:权限不足-目标为管理员"
                    )
                    raise PermissionError("普通管理员不能移除其他管理员或超级管理员")
            else:
                # 非管理员不能调用此方法（理论上Handler层应拦截，这里做防御性编程）
                raise PermissionError("权限不足")

            # 4. 执行删除
            self.db.delete_user(target_id)
            self.db.add_system_log("移除白名单", target_id, str(target_id), source)

    def get_user_lists_formatted(self):
        rows = self.db.list_users()
        admins_list, users_list = [], []
        # 辅助内部函数用于转义，防止破坏 Markdown 结构
        def escape_md_name(name):
            chars = ['_', '*', '[', ']', '`'] # 针对 Markdown V1/V2 的关键字符
            for c in chars:
                name = name.replace(c, f'\\{c}')
            return name
        for r in rows:
            name = escape_md_name(r["display_name"] or "未知")
            # ID 也是数字，一般安全，但如果是 user_input 导致非数字 ID 则需注意
            line = f"{name} (`{r['user_id']}`)" 
            
            if r["role"] in ("admin", "super_admin") and r["user_id"] not in self.super_admin_ids:
                admins_list.append(line)
            elif r["role"] == "user":
                users_list.append(line)
        return admins_list, users_list
    
    def get_recent_logs(self, limit=10):
        rows = self.db.get_recent_logs(limit)
        if not rows:
            return ["暂无日志记录"]
        lines = []
        for r in rows:
            line = f"[{r['ts']}] {r['action']} - 用户: {r['user_name']} (ID:{r['target_id']}) - 来源: {r['source']}\n"
            lines.append(line)
        return lines


class SettingsManager:
    def __init__(self, db):
        self.db = db

    def set_system_prompt(self, user_id, prompt, chat_type="private"):
        self.db.set_prompt(user_id, chat_type, prompt)

    def get_system_prompt(self, user_id, chat_type="private"):
        return self.db.get_prompt(user_id, chat_type)


class UsageManager:
    def __init__(self, db, auth_manager=None, cfg=None):
        self.db = db
        self.auth_manager = auth_manager
        self.cfg = cfg
        self._lock = threading.RLock()


    def record_usage(self, user_id, model_name, msg_delta=1, token_delta=0, ts=None):
        if ts is None:
            ts = int(time.time())
        with self._lock:
            self.db.incr_usage(user_id, model_name, msg_delta, token_delta, ts)


class RateLimiter:
    def __init__(self, db, auth_manager, cfg):
        self.db = db
        self.auth_manager = auth_manager
        self.cfg = cfg
        self._lock = threading.RLock()
        self._last_cleanup_timestamp = 0 

    def _get_current_keys(self):
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d-%H"), now.strftime("%Y-%m-%d")

    def _cleanup_old_records(self, user_id):
        now = datetime.datetime.now()
        hourly_keys = []
        daily_keys = []
        for i in range(24):
            hourly_keys.append((now - datetime.timedelta(hours=i)).strftime("%Y-%m-%d-%H"))
        for i in range(7):
            daily_keys.append((now - datetime.timedelta(days=i)).strftime("%Y-%m-%d"))
        self.db.cleanup_usage(user_id, "hourly", hourly_keys)
        self.db.cleanup_usage(user_id, "daily", daily_keys)

    def check_and_record(self, user_id, chat_type="private"):
        if not self.auth_manager.should_rate_limit(user_id, chat_type):
            return True, None
        hour_key, day_key = self._get_current_keys()
        with self._lock:
            # 2. 修改：不要每次都清理，每隔 3600秒 (1小时) 清理一次
            current_time = time.time()
            if current_time - self._last_cleanup_timestamp > 7200:
                # 这里为了不阻塞用户发消息，建议用线程去清理，或者就在这里清理也行（1小时卡顿一次没感觉）
                try:
                    self._cleanup_old_records(user_id)
                    self._last_cleanup_timestamp = current_time
                except Exception as e:
                    print(f"清理旧数据失败，但这不影响限流功能: {e}")
            hourly_count = self.db.get_usage(user_id, "hourly", hour_key)
            daily_count = self.db.get_usage(user_id, "daily", day_key)

            if hourly_count >= self.cfg.USER_RATE_LIMIT_HOURLY:
                remaining_minutes = 60 - datetime.datetime.now().minute
                return False, f"⏰ 已达到每小时 {self.cfg.USER_RATE_LIMIT_HOURLY} 次限制。\n请等待约 {remaining_minutes} 分钟后再试。"

            if daily_count >= self.cfg.USER_RATE_LIMIT_DAILY:
                return False, f"📅 已达到每天 {self.cfg.USER_RATE_LIMIT_DAILY} 次限制。\n请明天再来！"

            self.db.set_usage(user_id, "hourly", hour_key, hourly_count + 1)
            self.db.set_usage(user_id, "daily", day_key, daily_count + 1)
            return True, None

    def get_user_stats(self, user_id):
        if self.auth_manager.is_admin(user_id):
            return {"hourly_used": 0, "hourly_limit": "∞", "daily_used": 0, "daily_limit": "∞", "is_admin": True}
        hour_key, day_key = self._get_current_keys()
        hourly_count = self.db.get_usage(user_id, "hourly", hour_key)
        daily_count = self.db.get_usage(user_id, "daily", day_key)
        return {
            "hourly_used": hourly_count,
            "hourly_limit": self.cfg.USER_RATE_LIMIT_HOURLY,
            "daily_used": daily_count,
            "daily_limit": self.cfg.USER_RATE_LIMIT_DAILY,
            "is_admin": False
        }


class ChatQueueManager:
    def __init__(self, shutdown_event, log_exception):
        self._shutdown_event = shutdown_event
        self._log_exception = log_exception
        self._queues = {}
        self._last_active = {}
        self._idle_timeout = 600  # 秒
        self._lock = threading.RLock()

    def enqueue(self, chat_id_str, func, *args):
        with self._lock:
            q = self._queues.get(chat_id_str)
            if not q:
                q = queue.Queue()
                self._queues[chat_id_str] = q
                self._last_active[chat_id_str] = time.time()
                worker = threading.Thread(target=self._worker, args=(chat_id_str, q), daemon=True)
                worker.start()
            else:
                self._last_active[chat_id_str] = time.time()
        q.put((func, args))

    def _worker(self, chat_id_str, q):
        while not self._shutdown_event.is_set():
            try:
                func, args = q.get(timeout=0.5)
            except queue.Empty:
                with self._lock:
                    last = self._last_active.get(chat_id_str, 0)
                    if time.time() - last > self._idle_timeout:
                        if self._queues.get(chat_id_str) is q:
                            self._queues.pop(chat_id_str, None)
                            self._last_active.pop(chat_id_str, None)
                        return
                continue
            try:
                with self._lock:
                    self._last_active[chat_id_str] = time.time()
                func(*args)
            except Exception as e:
                self._log_exception(f"ChatQueueWorker chat_id={chat_id_str}", e)
            finally:
                q.task_done()


def check_and_prepare_task(context_manager, cfg, chat_id_str, chat_type, context):
    cooldown = context_manager.get_cooldown(chat_id_str)
    if cooldown > 0:
        context_manager.set_cooldown(chat_id_str, cooldown - 1)
        if len(context) > cfg.MAX_SAFETY_LIMIT:
            return None, context[-cfg.SUMMARY_TRIGGER_PRIVATE:]
        return None, None

    if len(context) > cfg.MAX_SAFETY_LIMIT:
        return None, context[-cfg.SUMMARY_TRIGGER_PRIVATE:]

    if chat_type != 'private':
        if len(context) > cfg.LIMIT_HISTORY_GROUP:
            return None, context[-cfg.LIMIT_HISTORY_GROUP:]
        return None, None

    if len(context) <= cfg.SUMMARY_TRIGGER_PRIVATE:
        return None, None

    split_index = len(context) - cfg.SUMMARY_RETAIN_PRIVATE
    if split_index <= 0:
        return None, None

    return context[:split_index], None


def apply_summary_success(context_manager, chat_id_str, msgs_to_summarize, summary_text):
    if not summary_text:
        return
    current_context = context_manager.get_context(chat_id_str)
    if not current_context:
        return
    msg_count = len(msgs_to_summarize)
    if len(current_context) < msg_count:
        return

    start_uuid = msgs_to_summarize[0].get('uuid')
    end_uuid = msgs_to_summarize[-1].get('uuid')
    current_start_uuid = current_context[0].get('uuid')
    current_end_uuid = current_context[msg_count - 1].get('uuid')

    if start_uuid and end_uuid and start_uuid == current_start_uuid and end_uuid == current_end_uuid:
        summary_node = {
            "role": "system",
            "content": f"【长期记忆/前情提要】：{summary_text}",
            "uuid": str(uuid.uuid4()),
            "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        remaining_context = current_context[msg_count:]
        new_context = [summary_node] + remaining_context
        context_manager.update_context(chat_id_str, new_context, force_save=True)


def _get_context_slice_for_reply(context_manager, chat_id_str, target_uuid):
    context = context_manager.get_context(chat_id_str)
    if not target_uuid:
        return context
    for i, msg in enumerate(context):
        if msg.get("uuid") == target_uuid:
            return context[:i + 1]
    return context


def _insert_ai_reply(context_manager, chat_id_str, user_msg_uuid, ai_msg_obj):
    context = context_manager.get_context(chat_id_str)
    if user_msg_uuid:
        for existing in context:
            if existing.get("reply_to") == user_msg_uuid:
                return
    insert_index = None
    if user_msg_uuid:
        for i, msg in enumerate(context):
            if msg.get("uuid") == user_msg_uuid:
                insert_index = i + 1
                break
    if insert_index is None or insert_index > len(context):
        context.append(ai_msg_obj)
    else:
        context.insert(insert_index, ai_msg_obj)
    context_manager.update_context(chat_id_str, context)


def _build_effective_system_prompt(settings_manager, cfg, user_id, chat_type):
    prompt_type = "private" if chat_type == "private" else "group"
    user_prompt = settings_manager.get_system_prompt(user_id, prompt_type)
    base_prompt = user_prompt or cfg.DEFAULT_SYSTEM_PROMPT or ""
    extra_prompt = cfg.EXTRA_SYSTEM_PROMPT or ""
    return prompt_type, user_prompt, base_prompt, extra_prompt, base_prompt + extra_prompt
