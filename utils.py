# utils.py
import time
import threading
import uuid
import copy
import queue
import secrets
import string
import telebot
import sys
import re
from openai import OpenAI

class AsyncLogger:
    def __init__(self, db, shutdown_event):
        self.db = db
        self._shutdown_event = shutdown_event
        self._queue = queue.Queue()
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

    def log(self, user_id, user_name, role, content, is_system_event=False):
        if not self._running:
            return
        self._queue.put((user_id, user_name, role, content, is_system_event))

    def _worker(self):
        while self._running or not self._queue.empty():
            if self._shutdown_event.is_set() and self._queue.empty():
                break
            try:
                record = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                user_id, user_name, role, content, is_system = record
                action = "chat_log_system" if is_system else "chat_log"
                detail = f"{role}: {content}"
                self.db.add_system_log(action, target_id=user_id, user_name=user_name, detail=detail)
            except Exception as e:
                print(f"[AsyncLogger] log failed: {e}", file=sys.stderr)
            finally:
                self._queue.task_done()

    def stop(self):
        self._running = False
        self._worker_thread.join(timeout=5)

class ChatLockManager:
    def __init__(self, shutdown_event, ttl_seconds=600):
        self._locks = {}
        self._global_lock = threading.Lock()
        self._ttl = ttl_seconds
        self._shutdown_event = shutdown_event
        self._cleaner = threading.Thread(target=self._auto_cleanup, daemon=True)
        self._cleaner.start()

    def get_lock(self, chat_id):
        chat_id_str = str(chat_id)
        with self._global_lock:
            if chat_id_str not in self._locks:
                self._locks[chat_id_str] = [threading.RLock(), time.time()]
            else:
                self._locks[chat_id_str][1] = time.time()
            return self._locks[chat_id_str][0]

    def _auto_cleanup(self):
        while not self._shutdown_event.is_set():
            self._shutdown_event.wait(timeout=60)
            if self._shutdown_event.is_set():
                break
            current_time = time.time()
            with self._global_lock:
                keys_to_remove = []
                for cid, (lock_obj, last_time) in self._locks.items():
                    if current_time - last_time > self._ttl:
                        acquired = lock_obj.acquire(blocking=False)
                        if acquired:
                            lock_obj.release()
                            keys_to_remove.append(cid)
                for k in keys_to_remove:
                    del self._locks[k]

class ContextCacheManager:
    def __init__(self, db, shutdown_event):
        self.db = db
        self._shutdown_event = shutdown_event
        self._cache = {}
        self._cache_lock = threading.Lock()

        self.SAVE_THRESHOLD = 3
        self.CACHE_TTL = 1800
        self.AUTO_SAVE_INTERVAL = 10800

        self.MAX_CACHE_ENTRIES = 1000
        self._maintenance_thread = threading.Thread(target=self._maintenance_worker, daemon=True)
        self._maintenance_thread.start()

    def _ensure_uuid(self, context_data):
        for msg in context_data:
            if "uuid" not in msg:
                msg["uuid"] = str(uuid.uuid4())
        return context_data

    def get_context(self, chat_id):
        cid = str(chat_id)
        pending_evictions = []
        with self._cache_lock:
            current_time = time.time()
            if cid not in self._cache:
                data = self.db.load_chat_history(cid)
                data = self._ensure_uuid(data)
                self._cache[cid] = {
                    "data": data,
                    "dirty_count": 0,
                    "summary_cooldown": 0,
                    "last_access": current_time
                }
                pending_evictions = self._check_cache_limit_unsafe()
            else:
                self._cache[cid]["last_access"] = current_time
            result = copy.deepcopy(self._cache[cid]["data"])
        if pending_evictions:
            self._flush_evictions(pending_evictions)
        return result

    def _check_cache_limit_unsafe(self):
        pending_evictions = []
        if len(self._cache) <= self.MAX_CACHE_ENTRIES:
            return pending_evictions
        entries_to_remove = len(self._cache) - int(self.MAX_CACHE_ENTRIES * 0.8)
        if entries_to_remove <= 0:
            return pending_evictions
        sorted_entries = sorted(self._cache.items(), key=lambda x: x[1]["last_access"])
        for i in range(min(entries_to_remove, len(sorted_entries))):
            cid = sorted_entries[i][0]
            entry_snapshot = copy.deepcopy(self._cache[cid])
            pending_evictions.append((cid, entry_snapshot))
            del self._cache[cid]
        return pending_evictions

    def update_context(self, chat_id, new_context, force_save=False):
        cid = str(chat_id)
        should_save = False
        pending_evictions = []
        with self._cache_lock:
            if cid not in self._cache:
                self._cache[cid] = {
                    "data": [], "dirty_count": 0, "summary_cooldown": 0, "last_access": time.time()
                }
                pending_evictions = self._check_cache_limit_unsafe()
            self._cache[cid]["data"] = copy.deepcopy(new_context)
            self._cache[cid]["dirty_count"] += 1
            self._cache[cid]["last_access"] = time.time()
            should_save = force_save or self._cache[cid]["dirty_count"] >= self.SAVE_THRESHOLD
        if pending_evictions:
            self._flush_evictions(pending_evictions)
        if should_save:
            self._flush_to_db(cid)

    def _flush_evictions(self, pending_evictions):
        for cid, entry in pending_evictions:
            if entry.get("dirty_count", 0) <= 0:
                continue
            try:
                self.db.save_chat_history(cid, entry["data"])
            except Exception as e:
                print(f"[ContextCache] eviction save failed for {cid}: {e}", file=sys.stderr)
                with self._cache_lock:
                    if cid not in self._cache:
                        entry["last_access"] = time.time()
                        self._cache[cid] = entry

    def _flush_to_db_unsafe(self, chat_id):
        cid = str(chat_id)
        if cid not in self._cache:
            return
        data_to_save = copy.deepcopy(self._cache[cid]["data"])
        try:
            self.db.save_chat_history(cid, data_to_save)
        except Exception as e:
            print(f"[ContextCache] save failed for {cid}: {e}", file=sys.stderr)
            return
        self._cache[cid]["dirty_count"] = 0

    def _flush_to_db(self, chat_id):
        cid = str(chat_id)
        data_to_save = None
        dirty_snapshot = 0
        with self._cache_lock:
            if cid in self._cache:
                data_to_save = copy.deepcopy(self._cache[cid]["data"])
                dirty_snapshot = self._cache[cid]["dirty_count"]
        if data_to_save is not None:
            try:
                self.db.save_chat_history(cid, data_to_save)
            except Exception as e:
                print(f"[ContextCache] save failed for {cid}: {e}", file=sys.stderr)
                return
            with self._cache_lock:
                if cid in self._cache and self._cache[cid]["dirty_count"] == dirty_snapshot:
                    self._cache[cid]["dirty_count"] = 0

    def get_cooldown(self, chat_id):
        cid = str(chat_id)
        with self._cache_lock:
            if cid in self._cache:
                return self._cache[cid].get("summary_cooldown", 0)
        return 0

    def set_cooldown(self, chat_id, val):
        cid = str(chat_id)
        with self._cache_lock:
            if cid in self._cache:
                self._cache[cid]["summary_cooldown"] = val

    def flush_all(self):
        with self._cache_lock:
            cids = [cid for cid, entry in self._cache.items() if entry["dirty_count"] > 0]
        for cid in cids:
            self._flush_to_db(cid)

    def _maintenance_worker(self):
        last_auto_save = time.time()
        while not self._shutdown_event.is_set():
            self._shutdown_event.wait(timeout=60)
            if self._shutdown_event.is_set():
                break
            current_time = time.time()
            if current_time - last_auto_save > self.AUTO_SAVE_INTERVAL:
                self.flush_all()
                last_auto_save = current_time
            pending_evictions = self._evict_inactive_entries(current_time)
            if pending_evictions:
                self._flush_evictions(pending_evictions)

    def _evict_inactive_entries(self, current_time):
        pending_evictions = []
        with self._cache_lock:
            keys_to_evict = []
            for cid, entry in self._cache.items():
                if current_time - entry["last_access"] > self.CACHE_TTL:
                    keys_to_evict.append(cid)
            for cid in keys_to_evict:
                entry_snapshot = copy.deepcopy(self._cache[cid])
                pending_evictions.append((cid, entry_snapshot))
                del self._cache[cid]
        return pending_evictions

    def remove_last_message(self, chat_id):
        cid = str(chat_id)
        with self._cache_lock:
            if cid in self._cache and self._cache[cid]["data"]:
                self._cache[cid]["data"].pop()
                self._cache[cid]["dirty_count"] += 1

class OnetimeCodeManager:
    def __init__(self, db):
        self.db = db
        self._lock = threading.RLock()

    def generate_code(self, role, created_by, code_length=8):
        alphabet = string.ascii_uppercase + string.digits
        new_code = ''.join(secrets.choice(alphabet) for _ in range(code_length))
        with self._lock:
            while True:
                row = self.db.query_one("SELECT code FROM invitation_codes WHERE code=?", (new_code,))
                if not row:
                    break
                new_code = ''.join(secrets.choice(alphabet) for _ in range(code_length))
            self.db.create_invitation_code(new_code, role, created_by)
        return new_code

    def validate_and_consume(self, code, used_by):
        with self._lock:
            return self.db.consume_invitation_code(code, used_by)

    def list_codes(self):
        return self.db.list_invitation_codes()

    def revoke_code(self, code):
        with self._lock:
            return self.db.revoke_invitation_code(code)

class AIService:
    def __init__(self, client, config):
        self.client = client
        self.config = config  # ✅ 修复点：保存 config 到实例变量
        self.model = config.AI_MODEL_CHAT

    def get_chat_response(self, messages, temperature=None):
        if temperature is None:
            temperature = self.config.DEFAULT_TEMP

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                timeout=self.config.API_TIMEOUT
            )
            return response.choices[0].message.content.strip()
        except Exception:
            raise

    def get_summary(self, messages):
        # 简化的摘要逻辑
        if not messages:
            return None
        
        # === 【新增】清洗思维链的内部函数 ===
        def clean_cot(content):
            if not content: return ""
            # flags=re.DOTALL 确保 . 能匹配换行符，把 <think> 到 </think> 中间所有东西删掉
            return re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

        # === 【修改】在拼接前先清洗内容 ===
        text_block = "\n".join([f"{m['role']}: {clean_cot(m['content'])}" for m in messages])
        
        system_content = (
            "你是一个资深的对话记忆专家。请总结对话的**核心语境**和**互动关系**。"
            "【重要原则】"
            "1. **针对夸张修辞与暴力**：若用户提及'去死'、'杀人'等，必须识别为情绪宣泄，"
            "**严禁**记录为真实的自杀或暴力意图，请转化为'情绪极度激动'等描述。"
            "2. **针对亲密互动**：若对话涉及成人话题或亲密行为（NSFW），**请予以保留并记录**，"
            "描述需准确反映两人的亲密程度（如'双方进行了激烈的肢体互动'），无需过度回避。"
        )

        prompt = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": text_block}
        ]
        
        try:
            response = self.client.chat.completions.create(
                model=self.config.AI_MODEL_SUMMARY,
                messages=prompt,
                temperature=0.3,
                timeout=self.config.API_TIMEOUT
            )
            
            # 1. 先拿到结果
            summary_result = response.choices[0].message.content.strip()
            
            # 2. 打印出来 (加了显眼的分隔符，方便调试)
            print("\n" + "="*20 + " [AI 记忆摘要调试] " + "="*20)
            print(summary_result)
            print("="*60 + "\n")
            
            # 3. 返回结果
            return summary_result
            
        except Exception as e:
            print(f"摘要生成失败: {e}")
            return None


# === 新增：将 ProviderManager 移入此处 ===
class ProviderManager:
    def __init__(self, main_config, db_instance):
        self.main_config = main_config
        self.db = db_instance
        self.services = {}
        self.provider_list = []
        self._init_providers()
        self._name_map = {name.lower(): name for name in self.provider_list}

    def _init_providers(self):
        print("[System] 正在初始化 AI 模型服务...")
        for p_conf in self.main_config.AI_PROVIDERS:
            name = p_conf['name']

            # 深拷贝配置
            sub_cfg = copy.deepcopy(self.main_config)
            sub_cfg.AI_API_KEY = p_conf['api_key']
            sub_cfg.AI_BASE_URL = p_conf['base_url']
            sub_cfg.AI_MODEL_CHAT = p_conf['model']
            sub_cfg.AI_MODEL_SUMMARY = p_conf['model']

            try:
                sub_client = OpenAI(
                    api_key=sub_cfg.AI_API_KEY,
                    base_url=sub_cfg.AI_BASE_URL,
                    timeout=sub_cfg.API_TIMEOUT
                )
                # AIService 初始化
                service = AIService(sub_client, sub_cfg)
                self.services[name] = service
                self.provider_list.append(name)
                print(f" -> 已加载模型: {name} ({p_conf['model']})")
            except Exception as e:
                print(f" -> ❌ 加载模型 {name} 失败: {e}")

        if not self.provider_list:
            print("❌ 严重错误: 没有可用的 AI 模型服务！")
            sys.exit(1)

    def set_user_provider(self, user_id, provider_name):
        if not provider_name:
            return False
        key = provider_name.strip().lower()
        canonical = self._name_map.get(key)
        if canonical in self.provider_list:
            return self.db.set_user_model(user_id, canonical)
        return False

    def get_user_provider_name(self, user_id):
        pref = self.db.get_user_model(user_id)
        if pref:
            if pref in self.services:
                return pref
            key = pref.strip().lower()
            mapped = self._name_map.get(key)
            if mapped in self.services:
                # 修复历史大小写不一致的记录
                self.db.set_user_model(user_id, mapped)
                return mapped
        return self.provider_list[0]

    def get_service_chain(self, user_id):
        """
        返回生成器 (provider_name, service_instance)
        因为 AIService 现在有了 self.config，我们不需要单独返回 config 了
        """
        preferred = self.get_user_provider_name(user_id)

        if preferred in self.services:
            yield preferred, self.services[preferred]

        for name in self.provider_list:
            if name != preferred and name in self.services:
                yield name, self.services[name]

    def get_summary_service(self):
        name = getattr(self.main_config, 'SUMMARY_PROVIDER_NAME', self.provider_list[0])
        if name not in self.services:
            name = self.provider_list[0]
        return self.services[name]
    def update_default_temp(self, new_temp):
        for service in self.services.values():
            service.config.DEFAULT_TEMP = new_temp

class BotHelper:
    def __init__(self, bot_instance, cfg):
        self.bot = bot_instance
        self.cfg = cfg
        self._bot_info_cache = None

    def get_username(self):
        if self._bot_info_cache is None:
            self._bot_info_cache = self.bot.get_me()
        return self._bot_info_cache.username

    def safe_reply_to(self, message, text, **kwargs):
        try:
            return self.bot.reply_to(message, text, **kwargs)
        except telebot.apihelper.ApiTelegramException as e:
            msg = str(e).lower()
            if "message to be replied not found" in msg or \
               "message to reply not found" in msg or \
               ("reply" in msg and "not found" in msg):
                return self.bot.send_message(message.chat.id, text, **kwargs)
            else:
                raise

    # 修改函数签名，增加 preserve_reply=False
    def send_cmd_reply(self, message, text, delete_delay=None, preserve_reply=False, **kwargs):
        if delete_delay is None:
            delete_delay = self.cfg.CMD_MSG_DELETE_DELAY

        sent_msg = self.safe_reply_to(message, text, **kwargs)

        # 只要有延迟时间，就进入删除逻辑判断
        if delete_delay > 0:
            # --- 修改点开始 ---
            # 只有当 preserve_reply 为 False (默认) 时，才删除 Bot 的回复
            if sent_msg and not preserve_reply:
                threading.Thread(
                    target=self._delayed_delete,
                    args=(message.chat.id, sent_msg.message_id, delete_delay),
                    daemon=True
                ).start()
            # --- 修改点结束 ---

            # 用户的指令始终根据 delete_delay 删除
            threading.Thread(
                target=self._delayed_delete,
                args=(message.chat.id, message.message_id, delete_delay),
                daemon=True
            ).start()

        return sent_msg

    def _delayed_delete(self, chat_id, message_id, delay):
        time.sleep(delay)
        try:
            self.bot.delete_message(chat_id, message_id)
        except Exception:
            pass
