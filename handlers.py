# handlers.py
import uuid
import datetime
def escape_md(text):
    if not text: return ""
    chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for c in chars:
        text = text.replace(c, f'\\{c}')
    return text

def register_handlers(
    bot,
    cfg,
    auth_manager,
    settings_manager,
    rate_limiter,
    usage_manager,
    context_manager,
    chat_locks,
    async_logger,
    onetime_code_manager,
    provider_manager,
    bot_helper,
    chat_queue_manager,
    log_exception,
    check_and_prepare_task,
    apply_summary_success,
    build_effective_system_prompt,
    get_context_slice_for_reply,
    insert_ai_reply,
):
    def core_reply_cycle(chat_id, user_id, message_to_reply, user_msg_uuid):
        chat_id_str = str(chat_id)
        chat_type = message_to_reply.chat.type
        chat_lock = chat_locks.get_lock(chat_id_str)

        prompt_type, user_prompt, base_prompt, extra_prompt, user_system_prompt = build_effective_system_prompt(
            settings_manager, cfg, user_id, chat_type
        )

        bot.send_chat_action(chat_id, 'typing')

        ai_reply = None
        success_provider = None
        usage = None
        error_log = []

        try:
            with chat_lock:
                api_context = get_context_slice_for_reply(context_manager, chat_id_str, user_msg_uuid)
                api_context = [{"role": m["role"], "content": m["content"]} for m in api_context]
                messages_payload = [{"role": "system", "content": user_system_prompt}] + api_context

            for p_name, service in provider_manager.get_service_chain(user_id):
                try:
                    temp = service.config.DEFAULT_TEMP
                    result = service.get_chat_response(messages_payload, temp)

                    if isinstance(result, tuple) and len(result) == 2:
                        ai_reply, usage = result
                    elif isinstance(result, dict):
                        ai_reply = result.get("content") or result.get("reply") or result.get("text")
                        usage = result.get("usage")
                    else:
                        ai_reply = result

                    if ai_reply:
                        success_provider = p_name
                        break
                except Exception as e:
                    err_msg = str(e)
                    print(f"[Failover] User:{user_id} | Provider:{p_name} 失败: {err_msg}")
                    error_log.append(f"{p_name}: {err_msg[:50]}...")
                    continue

            if not ai_reply:
                raise Exception(f"所有线路均失败: {'; '.join(error_log)}")

            sent = False
            try:
                bot_helper.safe_reply_to(message_to_reply, ai_reply)
                sent = True
            except Exception as e:
                log_exception("Telegram send_message", e)
                sent = False

            if sent:
                ai_msg_obj = {
                    "role": "assistant",
                    "content": ai_reply,
                    "uuid": str(uuid.uuid4()),
                    "reply_to": user_msg_uuid,
                    "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "model": success_provider
                }
                with chat_lock:
                    insert_ai_reply(context_manager, chat_id_str, user_msg_uuid, ai_msg_obj)

                if chat_type == 'private':
                    display_name = auth_manager.get_display_name(message_to_reply.from_user)
                    async_logger.log(user_id, display_name, f"Bot({success_provider})", ai_reply)

                token_delta = 0
                if usage:
                   token_delta = usage.get("total_tokens", (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)))

                usage_manager.record_usage(user_id, success_provider, msg_delta=1, token_delta=token_delta)

        except Exception as e:
            log_exception("Core reply", e)
            try:
                bot_helper.safe_reply_to(message_to_reply, "😵 连接 AI 服务失败，所有线路均无响应。")
            except Exception:
                pass

    def private_only(func):
        def wrapper(message):
            if message.chat.type != 'private':
                bot_helper.send_cmd_reply(message, "⚠️ 此指令只能在私聊中使用。")
                return
            return func(message)
        return wrapper
    
    def require_whitelist(func):
        def wrapper(message):
            if not auth_manager.is_whitelisted(message.from_user.id):
                return
            return func(message)
        return wrapper

    def require_admin(func):
        def wrapper(message):
            if not auth_manager.is_admin(message.from_user.id):
                return
            return func(message)
        return wrapper

    def require_super_admin(func):
        def wrapper(message):
            if not auth_manager.is_super_admin(message.from_user.id):
                return
            return func(message)
        return wrapper

    def _get_chat_type(message):
        return "private" if message.chat.type == "private" else "group"

    def _get_cmd_arg(message, idx, usage_text):
        args = message.text.split()
        if len(args) <= idx:
            bot_helper.send_cmd_reply(message, usage_text, parse_mode="Markdown")
            return None
        return args[idx]

    def _reply_unauthorized(message, user_id):
        msg = f"⛔ 未授权。ID: `{user_id}`\n🔑 请使用 `/auth 邀请码` 进行认证。"
        bot_helper.safe_reply_to(message, msg, parse_mode="Markdown")

    def _handle_set_prompt(message, prompt_type, cmd_hint):
        user_id = message.from_user.id
        cmd_used = message.text.split()[0]
        prompt_text = message.text.replace(cmd_used, "").strip()

        if not prompt_text:
            current = settings_manager.get_system_prompt(user_id, prompt_type)
            label = "私聊" if prompt_type == "private" else "群组"
            msg = f"🎭 *当前{label}人设提示词*:\n"
            msg += f"`{current}`" if current else "`默认 (坏心眼助手)`"
            msg += f"\n\n*设置方法*: `{cmd_hint}` 提示词\n(限{cfg.MAX_PROMPT_LENGTH}字以内，用 `reset` 可恢复默认)"
            bot_helper.send_cmd_reply(message, msg, parse_mode="Markdown")
            return

        if prompt_text.lower() == "reset":
            settings_manager.set_system_prompt(user_id, None, prompt_type)
            bot_helper.send_cmd_reply(message, f"✅ 已恢复{('私聊' if prompt_type=='private' else '群组')}默认人设。")
            return

        if len(prompt_text) > cfg.MAX_PROMPT_LENGTH:
            bot_helper.send_cmd_reply(
                message,
                f"❌ 提示词太长了 ({len(prompt_text)}字)。请控制在 {cfg.MAX_PROMPT_LENGTH} 字以内。"
            )
            return

        settings_manager.set_system_prompt(user_id, prompt_text, prompt_type)
        bot_helper.send_cmd_reply(
            message,
            f"✅ {('私聊' if prompt_type=='private' else '群组')}人设已更新！AI现在会根据以下设定回复：\n`{prompt_text}`",
            parse_mode="Markdown"
        )

    def validate_invitation_code(input_code, user_id):
        role = onetime_code_manager.validate_and_consume(input_code, user_id)
        if role:
            return role, "一次性邀请码"
        role = cfg.INVITATION_CODES.get(input_code)
        if role:
            return role, "永久邀请码"
        return None, None

    @bot.message_handler(commands=['help'])
    @private_only
    def cmd_help(message):
        chat_type = _get_chat_type(message)
        user_id = message.from_user.id
        if not auth_manager.can_use_chat(user_id, chat_type):
            help_text = "🔒 *Bot 访问受限*\n\n此Bot仅限授权用户使用。"
            help_text += "\n\n🔑 如果你有邀请码，请使用：\n`/auth 邀请码`"
            bot_helper.send_cmd_reply(message, help_text, parse_mode="Markdown", preserve_reply=True)
            return

        help_text = """📚 *指令帮助菜单*

*⚠️ 除 /clear 外，所有指令只能在私聊中使用*

*👤 常用指令*

`/sp` 设置私聊时AI的性格。
用 `/sp reset` 恢复默认。
例: `/sp 你是一只猫娘`

`/sg` 设置群聊时AI的性格。
用 `/sg reset` 恢复默认。
例: `/sg 你是群里的吉祥物`

`/clear`
清空当前对话的记忆（群组中仅管理员可用）

`/usage`
查看今日/本小时使用次数

`/model`
查看或切换 AI 模型/线路

`/sys`
查看当前生效的系统提示词
"""
        if auth_manager.is_admin(user_id):
            help_text += """
*👮 管理员指令*

`/add` 用户ID
添加白名单。也可回复某人消息直接使用 `/add`

`/del` 用户ID  
移除白名单。

`/recent_users`
查看最近加入白名单的日志

`/temp` 温度值
调整AI温度（0.0-2.0）
"""
        if auth_manager.is_super_admin(user_id):
            help_text += """
*👑 超级管理员指令*

`/gc user` 生成普通用户一次性邀请码
`/gc admin` 生成管理员一次性邀请码

`/gl user` 生成普通用户一次性邀请链接
`/gl admin` 生成管理员一次性邀请链接

`/lc` 查看所有未使用的一次性邀请码
`/rmc 邀请码` 撤销一个未使用的邀请码

`/add_admin` 用户ID 直接添加管理员
"""
        bot_helper.send_cmd_reply(message, help_text, parse_mode="Markdown", preserve_reply=True)

    @bot.message_handler(commands=['model', 'switch'])
    @private_only
    @require_whitelist
    def cmd_switch_model(message):
        user_id = message.from_user.id
        # 1. 优化参数解析，支持带空格的模型名
        cmd_text = message.text.strip()
        args = cmd_text.split(maxsplit=1)
        
        # 如果没有参数，显示列表
        if len(args) < 2:
            current = provider_manager.get_user_provider_name(user_id)
            
            # 使用列表构建，最后再一次性 join，性能更好
            lines = [f"🤖 *当前使用模型*: `{current}`", "", "*可用模型列表*:"]
            
            for p in cfg.AI_PROVIDERS:
                name = p['name']
                # 获取描述，如果没有则用名字代替
                raw_desc = p.get('description', name)
                
                # 【关键修复】对描述进行 Markdown 转义，防止 _ * 等符号导致不响应
                # 注意：handlers.py 顶部必须有 escape_md 函数
                safe_desc = escape_md(raw_desc)
                
                status = "✅" if name == current else "⚪️"
                
                # 检查是否实际加载
                is_loaded = any(loaded_name.lower() == name.lower() for loaded_name in provider_manager.provider_list)
                if not is_loaded:
                    status = "❌(未加载)"
                
                lines.append(f"{status} `{name}` - {safe_desc}")
                
            lines.append("\n*切换指令*: `/model 模型名`\n例: `/model Qwen`")
            
            msg = "\n".join(lines)
            bot_helper.send_cmd_reply(message, msg, parse_mode="Markdown", preserve_reply=True)
            return

        # 切换逻辑
        input_name = args[1].strip().lower()
        target_model = None
        
        for name in provider_manager.provider_list:
            if name.lower() == input_name:
                target_model = name
                break
        
        if target_model:
            if provider_manager.set_user_provider(user_id, target_model):
                # 同样获取描述并转义
                desc_str = target_model
                for p in cfg.AI_PROVIDERS:
                    if p['name'] == target_model:
                        desc_str = p.get('description', target_model)
                        break
                safe_desc = escape_md(desc_str)
                
                bot_helper.send_cmd_reply(message, f"✅ 切换成功！\n现在使用: *{safe_desc}* (`{target_model}`)", parse_mode="Markdown")
            else:
                bot_helper.send_cmd_reply(message, "❌ 切换失败，内部错误。")
        else:
            bot_helper.send_cmd_reply(message, f"❌ 找不到模型 `{args[1]}`。\n请检查拼写或确认该模型是否显示为 ✅。")


    @bot.message_handler(commands=['start'])
    @private_only
    def cmd_start(message):
        chat_type = _get_chat_type(message)
        user_id = message.from_user.id
        args = message.text.split()
        welcome_text = f"👋 你好！我是 {cfg.DESCRIPTION}。"

        if auth_manager.can_use_chat(user_id, chat_type):
            bot_helper.send_cmd_reply(message, f"{welcome_text}\n✅ 你已经拥有使用权限，直接发送消息即可聊天。")
            return

        if len(args) > 1:
            input_code = args[1]
            role, source = validate_invitation_code(input_code, user_id)
            if role:
                if role == "admin":
                    # 邀请码为 admin 时，直接授予管理员角色，不走超级管理员限制
                    auth_manager.add_admin_by_invite(
                        user_id,
                        source=f"{source}(Link)-Admin",
                        user_obj=message.from_user
                    )
                    bot_helper.send_cmd_reply(
                        message,
                        "🎉 认证成功！你已获得 **管理员** 权限。",
                        parse_mode="Markdown"
                    )
                else:
                    # 普通邀请码走现有白名单逻辑
                    auth_manager.add_user(
                        user_id,
                        source=f"{source}(Link)-User",
                        user_obj=message.from_user
                    )
                    bot_helper.send_cmd_reply(
                        message,
                        "🎉 认证成功！你已自动加入白名单，现在可以开始聊天了。"
                    )
                return
            bot_helper.send_cmd_reply(message, "❌ 邀请链接无效或已过期。")
            return

        msg = f"{welcome_text}\n⛔ 目前仅限授权用户使用。"
        msg += "\n🔑 如果你有邀请码，请发送 `/auth 邀请码` 进行认证。"
        bot_helper.send_cmd_reply(message, msg, parse_mode="Markdown")

    @bot.message_handler(commands=['auth'])
    @private_only
    def cmd_auth(message):
        chat_type = _get_chat_type(message)
        user_id = message.from_user.id
        if auth_manager.can_use_chat(user_id, chat_type):
            bot_helper.send_cmd_reply(message, "✅ 你已经在白名单中，无需重复认证。")
            return
        args = message.text.split()
        if len(args) < 2:
            bot_helper.send_cmd_reply(message, "⚠️ 请输入邀请码。用法: `/auth 邀请码`", parse_mode="Markdown")
            return
        input_code = args[1]
        role, source = validate_invitation_code(input_code, user_id)
        if role:
            if role == "admin":
                # 邀请码为 admin 时，直接授予管理员角色，不走超级管理员限制
                auth_manager.add_admin_by_invite(
                    user_id,
                    source=f"{source}(Auth)-Admin",
                    user_obj=message.from_user
                )
                bot_helper.send_cmd_reply(
                    message,
                    "🎉 认证成功！你已获得 **管理员** 权限。",
                    parse_mode="Markdown"
                )
            else:
                # 普通邀请码走现有白名单逻辑
                auth_manager.add_user(
                    user_id,
                    source=f"{source}(Auth)-User",
                    user_obj=message.from_user
                )
                bot_helper.send_cmd_reply(
                    message,
                    "🎉 认证成功！你已加入白名单。",
                    preserve_reply=True
                )
        else:
            bot_helper.send_cmd_reply(message, "❌ 邀请码错误或已被使用。")

    @bot.message_handler(commands=['sys'])
    @private_only
    @require_whitelist
    def cmd_show_system_prompt(message):
        user_id = message.from_user.id
        
        # 1. 获取用户自定义的设定
        private_prompt = settings_manager.get_system_prompt(user_id, "private")
        group_prompt = settings_manager.get_system_prompt(user_id, "group")

        # 2. 获取系统默认设定 (关键步骤：先定义变量)
        # 确保你的 config.py 里确实有 DEFAULT_SYSTEM_PROMPT 这个变量
        default_val = cfg.DEFAULT_SYSTEM_PROMPT

        parts = ["🧠 *当前系统提示词设定*"]

        # --- 显示私聊设定 ---
        parts.append("\n👤 *私聊模式 (/sp)*:")
        if private_prompt:
            parts.append(f"```\n{private_prompt}\n```")
        else:
            # 如果没设置，显示默认值
            parts.append(f"(默认):\n```\n{default_val}\n```")

        # --- 显示群聊设定 ---
        parts.append("\n👥 *群聊模式 (/sg)*:")
        if group_prompt:
            parts.append(f"```\n{group_prompt}\n```")
        else:
            # 如果没设置，显示默认值
            parts.append(f"(默认):\n```\n{default_val}\n```")
            
        bot_helper.send_cmd_reply(message, "\n".join(parts), parse_mode="Markdown", preserve_reply=True)

    @bot.message_handler(commands=['gc'])
    @private_only
    @require_super_admin
    def cmd_gc(message):
        role_arg = _get_cmd_arg(
            message, 1,
            "⚠️ 请指定权限类型。\n\n用法:\n`/gc user` - 生成普通用户邀请码\n`/gc admin` - 生成管理员邀请码")
        if not role_arg:
            return
        role_arg = role_arg.lower()

        if role_arg not in ["user", "admin"]:
            bot_helper.send_cmd_reply(message, "❌ 权限类型必须是 `user` 或 `admin`", parse_mode="Markdown")
            return
        new_code = onetime_code_manager.generate_code(role_arg, message.from_user.id, cfg.ONETIME_CODE_LENGTH)
        role_display = "👤 普通用户" if role_arg == "user" else "👮 管理员"
        bot_helper.send_cmd_reply(
            message,
            f"✅ 一次性邀请码已生成\n\n"
            f"📋 邀请码: `{new_code}`\n"
            f"🔐 权限: {role_display}\n\n"
            f"⚠️ 此邀请码只能使用一次，使用后自动失效。",
            parse_mode="Markdown", preserve_reply=True
        )

    @bot.message_handler(commands=['gl'])
    @private_only
    @require_super_admin
    def cmd_gl(message):
        role_arg = _get_cmd_arg(
            message, 1,
            "⚠️ 请指定权限类型。\n\n用法:\n`/gl user` - 生成普通用户邀请链接\n`/gl admin` - 生成管理员邀请链接")
        if not role_arg:
            return
        role_arg = role_arg.lower()

        if role_arg not in ["user", "admin"]:
            bot_helper.send_cmd_reply(message, "❌ 权限类型必须是 `user` 或 `admin`", parse_mode="Markdown")
            return
        new_code = onetime_code_manager.generate_code(role_arg, message.from_user.id, cfg.ONETIME_CODE_LENGTH)
        bot_username = bot_helper.get_username()
        invite_link = f"https://t.me/{bot_username}?start={new_code}"
        invite_link_display = invite_link.replace("_", "\\_")
        role_display = "👤 普通用户" if role_arg == "user" else "👮 管理员"
        bot_helper.send_cmd_reply(
            message,
            f"✅ 一次性邀请链接已生成\n\n"
            f"🔗 链接: {invite_link_display}\n"
            f"🔐 权限: {role_display}\n\n"
            f"⚠️ 此链接只能使用一次，使用后自动失效。",
            parse_mode="Markdown", preserve_reply=True
        )

    @bot.message_handler(commands=['lc'])
    @private_only
    @require_super_admin
    def cmd_lc(message):
        codes = onetime_code_manager.list_codes()
        if not codes:
            bot_helper.send_cmd_reply(message, "📋 当前没有未使用的一次性邀请码。")
            return
        lines = ["📋 *未使用的一次性邀请码*\n"]
        for info in codes:
            role_emoji = "👮" if info["role"] == "admin" else "👤"
            lines.append(f"{role_emoji} `{info['code']}` - {info['created_at']}")
        bot_helper.send_cmd_reply(message, "\n".join(lines), parse_mode="Markdown")

    @bot.message_handler(commands=['rmc'])
    @private_only
    @require_super_admin
    def cmd_rmc(message):
        code_to_revoke = _get_cmd_arg(message, 1, "⚠️ 请指定要撤销的邀请码。用法: `/rmc 邀请码`")
        if not code_to_revoke:
            return
        if onetime_code_manager.revoke_code(code_to_revoke):
            bot_helper.send_cmd_reply(message, f"✅ 邀请码 `{code_to_revoke}` 已撤销。", parse_mode="Markdown")
        else:
            bot_helper.send_cmd_reply(message, f"❌ 邀请码 `{code_to_revoke}` 不存在或已被使用。", parse_mode="Markdown")

    @bot.message_handler(commands=['recent_users', 'logs'])
    @private_only
    @require_admin
    def cmd_recent_users(message):
        logs = auth_manager.get_recent_logs(limit=10)
        log_text = "".join(logs)
        bot_helper.send_cmd_reply(message, f"📜 *最近白名单变动记录*:\n\n```\n{log_text}```", parse_mode="Markdown")

    @bot.message_handler(commands=['set_private', 'sp'])
    @private_only
    @require_whitelist
    def cmd_set_private(message):
        _handle_set_prompt(message, "private", "/sp")

    @bot.message_handler(commands=['set_group', 'sg'])
    @private_only
    @require_whitelist
    def cmd_set_group(message):
        _handle_set_prompt(message, "group", "/sg")

    @bot.message_handler(commands=['add_admin'])
    @private_only
    @require_super_admin
    def cmd_add_admin(message):
        try:
            arg = _get_cmd_arg(
                message, 1,
                "⚠️ 格式错误。用法: `/add_admin` 用户ID\n例: `/add_admin 12345678`"
            )
            if not arg:
                return
            target_id = int(arg)
            auth_manager.add_admin(target_id, operator_id=message.from_user.id)
            bot_helper.send_cmd_reply(message, f"✅ 已将 ID `{target_id}` 设为管理员。", parse_mode="Markdown")
        except ValueError:
            bot_helper.send_cmd_reply(message, "❌ ID 必须是数字。")

    @bot.message_handler(commands=['add'])
    @private_only
    @require_admin
    def cmd_add_user(message):
        target_id = None
        target_name = "ID用户"
        target_user_obj = None
        if message.reply_to_message:
            target_id = message.reply_to_message.from_user.id
            target_user_obj = message.reply_to_message.from_user
            target_name = auth_manager.get_display_name(target_user_obj)
        else:
            args = message.text.split()
            if len(args) >= 2 and args[1].isdigit():
                target_id = int(args[1])
                target_name = str(target_id)
            else:
                bot_helper.send_cmd_reply(message, "⚠️ 使用方法：\n1. 回复某人的消息发送 `/add`\n2. 发送 `/add` 用户ID\n例: `/add 12345678`", parse_mode="Markdown")
                return
        auth_manager.add_user(target_id, source="管理员添加", user_obj=target_user_obj)
        bot_helper.send_cmd_reply(message, f"✅ 已添加白名单: {target_name} (`{target_id}`)", parse_mode="Markdown")

    @bot.message_handler(commands=['del'])
    @private_only
    @require_admin
    def cmd_del_user(message):
        user_id = message.from_user.id
        target_id = None
        target_name = "ID用户"
        if message.reply_to_message:
            target_id = message.reply_to_message.from_user.id
            target_name = auth_manager.get_display_name(message.reply_to_message.from_user)
        else:
            args = message.text.split()
            if len(args) >= 2 and args[1].isdigit():
                target_id = int(args[1])
                target_name = str(target_id)
            else:
                bot_helper.send_cmd_reply(message, "⚠️ 使用方法：\n1. 回复某人的消息发送 `/del`\n2. 发送 `/del` 用户ID\n例: `/del 12345678`", parse_mode="Markdown")
                return
        if auth_manager.get_role(target_id) == "admin" and not auth_manager.is_super_admin(user_id):
            bot_helper.send_cmd_reply(message, "⛔ 你没有权限删除其他管理员。")
            return
        if auth_manager.is_super_admin(target_id):
            bot_helper.send_cmd_reply(message, "⛔ 无法删除超级管理员。")
            return

        auth_manager.del_user(target_id, operator_id=user_id, source="管理员移除")
        bot_helper.send_cmd_reply(message, f"🗑️ 已移除权限: {target_name} (`{target_id}`)", parse_mode="Markdown")

    @bot.message_handler(commands=['temp'])
    @private_only
    @require_admin
    def cmd_set_temp(message):
        try:
            arg = _get_cmd_arg(
                message, 1,
                f"当前温度: `{cfg.DEFAULT_TEMP}`\n用法: `/temp` 温度值\n例: `/temp 0.8`"
            )
            if not arg:
                return
            new_temp = float(arg)
            if 0.0 <= new_temp <= 2.0:
                cfg.DEFAULT_TEMP = new_temp
                provider_manager.update_default_temp(new_temp)
                bot_helper.send_cmd_reply(message, f"🌡️ AI温度已设置为: `{new_temp}`", parse_mode="Markdown")
            else:
                bot_helper.send_cmd_reply(message, "⚠️ 温度必须在 0.0 到 2.0 之间。")
        except ValueError:
            bot_helper.send_cmd_reply(message, "❌ 请输入有效的数字。")

    @bot.message_handler(commands=['list'])
    @private_only
    @require_admin
    def cmd_list_users(message):
        admins_list, users_list = auth_manager.get_user_lists_formatted()
        pass
        admins_str = "\n".join(admins_list) or "无"
        users_str = "\n".join(users_list) or "无"
        super_admins = "\n".join(f"`{uid}`" for uid in sorted(auth_manager.super_admin_ids)) or "无"

        msg = (f"📋 *用户列表*\n\n"
               f"👑 *超级管理员*:\n{super_admins}\n\n"
               f"👮 *普通管理员*:\n{admins_str}\n\n"
               f"👤 *白名单用户*:\n{users_str}")

        if len(msg) > 4000:
            msg = msg[:4000] + "\n...(列表过长截断)"
        bot_helper.send_cmd_reply(message, msg, parse_mode="Markdown")

    @bot.message_handler(commands=['clear'])
    def clear_context(message):
        user_id = message.from_user.id
        chat_id = str(message.chat.id)
        chat_type = message.chat.type
        display_name = auth_manager.get_display_name(message.from_user)

        if not auth_manager.can_use_chat(user_id, chat_type):
            return
        if chat_type in ['group', 'supergroup']:
            if not auth_manager.is_admin(user_id):
                bot_helper.send_cmd_reply(message, "⛔ 只有管理员可以使用 /clear 指令。")
                return

        with chat_locks.get_lock(chat_id):
            context_manager.update_context(chat_id, [], force_save=True)
            context_manager.set_cooldown(chat_id, 0)

        if chat_type == 'private':
            async_logger.log(user_id, display_name, "System", "用户执行了 /clear 指令，记忆已重置", is_system_event=True)

        bot_helper.send_cmd_reply(message, "🧹 我们的回忆已清空，现在重新开始吧。")

    @bot.message_handler(commands=['version', 'ver', 'v'])
    @private_only
    def show_version(message):
        bot_helper.send_cmd_reply(
            message,
            f"🤖 *AI助手版本信息*\n"
            f"版本号: `{cfg.VERSION}`\n"
            f"构建日期: `{cfg.BUILD_DATE}`\n"
            f"功能描述: {cfg.DESCRIPTION}",
            parse_mode="Markdown"
        )

    @bot.message_handler(commands=['usage', 'quota', 'limit'])
    @private_only
    @require_whitelist
    def cmd_check_usage(message):
        chat_type = _get_chat_type(message)
        user_id = message.from_user.id
        stats = rate_limiter.get_user_stats(user_id)
        if stats["is_admin"]:
            msg = "👑 管理员无使用限制"
        else:
            hourly_remaining = stats["hourly_limit"] - stats["hourly_used"]
            daily_remaining = stats["daily_limit"] - stats["daily_used"]
            msg = (f"📊 *您的使用统计*\n\n"
                   f"⏰ 本小时: `{stats['hourly_used']}/{stats['hourly_limit']}` (剩余 {hourly_remaining} 次)\n"
                   f"📅 今日: `{stats['daily_used']}/{stats['daily_limit']}` (剩余 {daily_remaining} 次)")
        bot_helper.send_cmd_reply(message, msg, parse_mode="Markdown")

    @bot.message_handler(func=lambda message: True)
    def handle_message(message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        chat_id_str = str(chat_id)
        user_input = (message.text or message.caption or "").strip()
        chat_type = message.chat.type
        display_name = auth_manager.get_display_name(message.from_user)

        if auth_manager.can_use_chat(user_id, chat_type):
            auth_manager.update_user_info(user_id, display_name)

        if not auth_manager.can_use_chat(user_id, chat_type):
            if chat_type == 'private':
                _reply_unauthorized(message, user_id)
            return
        if not user_input:
            if chat_type == 'private':
                bot_helper.safe_reply_to(message, "⚠️ 暂不支持该类型消息，请发送文字。")
            return

        should_reply = False
        bot_username = bot_helper.get_username()

        if chat_type == 'private':
            should_reply = True
        else:
            if bot_username and f"@{bot_username}" in user_input:
                should_reply = True
                user_input = user_input.replace(f"@{bot_username}", "").strip()
            elif message.reply_to_message and \
                 message.reply_to_message.from_user and \
                 message.reply_to_message.from_user.username == bot_username:
                should_reply = True

        if not should_reply:
            return

        allowed, error_msg = rate_limiter.check_and_record(user_id, chat_type)
        if not allowed:
            bot_helper.safe_reply_to(message, error_msg)
            return

        chat_lock = chat_locks.get_lock(chat_id_str)
        msgs_to_summarize = None

        user_msg_uuid = str(uuid.uuid4())
        if chat_type != 'private':
            content_with_identity = f"[{display_name} (ID:{user_id})]: {user_input}"
            user_msg_obj = {"role": "user", "content": content_with_identity, "uuid": user_msg_uuid}
        else:
            user_msg_obj = {"role": "user", "content": user_input, "uuid": user_msg_uuid}

        with chat_lock:
            context = context_manager.get_context(chat_id_str)
            context.append(user_msg_obj)
            task_msgs, forced_context = check_and_prepare_task(context_manager, cfg, chat_id_str, chat_type, context)

            if forced_context:
                context = forced_context
                context_manager.update_context(chat_id_str, context, force_save=True)
                msgs_to_summarize = None
            else:
                context_manager.update_context(chat_id_str, context)
                msgs_to_summarize = task_msgs

        if chat_type == 'private':
            async_logger.log(user_id, display_name, "User", user_input)

        if msgs_to_summarize:
            bot.send_chat_action(chat_id, 'typing')
            svc = provider_manager.get_summary_service()
            summary_result = svc.get_summary(msgs_to_summarize)

            if summary_result:
                with chat_lock:
                    apply_summary_success(context_manager, chat_id_str, msgs_to_summarize, summary_result)
            else:
                with chat_lock:
                    context_manager.set_cooldown(chat_id_str, 5)

        chat_queue_manager.enqueue(chat_id_str, core_reply_cycle, chat_id, user_id, message, user_msg_uuid)
