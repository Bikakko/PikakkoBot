说明书也是AI写的

# 🤖 Telegram AI Chatbot Core

这是一个基于 Python (`pyTelegramBotAPI`) 开发的高级 Telegram AI 聊天机器人核心程序。它不仅仅是一个简单的 API 转发器，而是一个具备**长期记忆**、**多模型故障转移**、**精细化权限控制**和**高并发处理能力**的生产级系统。

## ✨ 主要功能 (Key Features)

### 🧠 智能对话与记忆
*   **上下文记忆**：自动维护对话上下文，支持连续对话。
*   **智能摘要 (Memory Summarization)**：当对话历史过长时，自动触发后台摘要任务，将旧的对话压缩为“长期记忆/前情提要”，在节省 Token 的同时保持人设和语境不丢失。
*   **思维链清洗**：针对推理模型（如 DeepSeek R1），内置逻辑自动清洗 `<think>` 标签，只向用户展示最终回复。
*   **多模型支持 (Multi-Provider)**：支持配置多个 OpenAI 兼容的 API 源（如 GPT-4, Claude, DeepSeek, Qwen 等）。
*   **自动故障转移 (Failover)**：如果当前配置的 AI 线路失败，系统会自动尝试列表中的下一个可用线路，确保回复的高可用性。

### 🛡️ 权限与安全
*   **白名单机制**：默认仅限授权用户使用。
*   **邀请码系统**：
    *   支持生成一次性邀请码 (`/gc`) 或邀请链接 (`/gl`)。
    *   支持区分“普通用户”和“管理员”权限的邀请码。
*   **分级权限**：
    *   **User**: 普通对话，受速率限制。
    *   **Admin**: 管理白名单，调整 AI 温度，无速率限制。
    *   **Super Admin**: 生成邀请码，管理管理员，查看系统日志。

### ⚙️ 个性化与控制
*   **自定义人设 (System Prompt)**：用户可以分别为“私聊”和“群组”设置不同的 AI 人设（例如：`/sp 你是一只猫娘`）。
*   **模型切换**：用户可以通过 `/model` 指令实时切换想使用的 AI 模型。
*   **速率限制 (Rate Limiting)**：内置每小时/每天的消息条数限制，防止滥用。
*   **群组友好**：在群组中支持通过 @机器人 或 回复机器人消息来触发，避免干扰正常聊天。

### 🔧 架构特性
*   **线程安全**：使用 `ChatLockManager` 和 `ChatQueueManager` 确保同一用户的消息按顺序处理，避免并发导致的上下文错乱。
*   **异步日志**：所有关键操作和对话均通过异步队列写入数据库，不阻塞主线程。
*   **缓存管理**：实现了 `ContextCacheManager`，将活跃对话缓存在内存中，定期自动持久化到数据库，兼顾速度与数据安全。
*   **自动清理**：指令消息回复后可自动删除，保持聊天界面整洁。

---

## 📂 文件结构说明

该项目核心逻辑由以下三个文件组成：

1.  **`handlers.py` (控制器层)**
    *   **作用**：Telegram 机器人的入口，负责路由所有的指令（Commands）和消息。
    *   **内容**：定义了 `/start`, `/help`, `/model`, `/clear` 等指令的处理逻辑；核心的 `core_reply_cycle` 函数负责组装提示词、调用 AI 服务并发送回复。

2.  **`services.py` (业务逻辑层)**
    *   **作用**：封装具体的业务规则和 AI 交互逻辑。
    *   **核心类**：
        *   `AuthManager`: 处理用户认证、白名单、管理员逻辑。
        *   `ProviderManager` & `AIService`: 管理 AI 模型列表，执行 API 调用，处理思维链清洗。
        *   `RateLimiter`: 执行用户请求频率限制。
        *   `ChatQueueManager`: 消息队列管理，防止并发冲突。
        *   `SettingsManager`: 管理用户自定义提示词。

3.  **`utils.py` (工具/基础层)**
    *   **作用**：提供底层基础设施支持。
    *   **核心类**：
        *   `ContextCacheManager`: 复杂的上下文缓存策略（LRU 剔除、定时保存）。
        *   `AsyncLogger`: 异步系统日志记录。
        *   `ChatLockManager`: 分布式锁（基于内存）实现。
        *   `BotHelper`: Telegram API 的安全封装（防报错、自动删除消息）。

---

## 📝 指令列表 (Commands)

### 👤 用户指令 (User)
| 指令 | 说明 |
| :--- | :--- |
| `/start [邀请码]` | 开始使用，或通过邀请码激活权限 |
| `/auth [邀请码]` | 使用邀请码进行认证 |
| `/clear` | **[常用]** 清空当前对话记忆，开始新话题 |
| `/model` | 查看可用模型或切换模型 (e.g., `/model gpt-4`) |
| `/sp [提示词]` | 设置**私聊**时的 AI 人设 (Set Private prompt) |
| `/sg [提示词]` | 设置**群聊**时的 AI 人设 (Set Group prompt) |
| `/usage` | 查看今日和本小时的使用量配额 |
| `/sys` | 查看当前生效的系统提示词 |

### 👮 管理员指令 (Admin)
| 指令 | 说明 |
| :--- | :--- |
| `/add [ID]` | 添加用户到白名单 (或回复消息使用) |
| `/del [ID]` | 移除用户权限 (或回复消息使用) |
| `/temp [0.0-2.0]` | 动态调整 AI 的温度值 (创造力) |
| `/recent_users` | 查看最近的白名单变动日志 |
| `/list` | 列出所有用户和管理员 |

### 👑 超级管理员指令 (Super Admin)
| 指令 | 说明 |
| :--- | :--- |
| `/gc [user/admin]` | 生成一次性邀请码 (Generate Code) |
| `/gl [user/admin]` | 生成一次性邀请链接 (Generate Link) |
| `/lc` | 列出所有未使用的邀请码 (List Codes) |
| `/rmc [邀请码]` | 撤销某个邀请码 |
| `/add_admin [ID]` | 直接添加新的管理员 |

---

## 🚀 部署依赖 (Requirements)

虽然本仓库仅展示了核心逻辑，但运行该程序通常需要：

*   Python 3.8+
*   `pyTelegramBotAPI` (Telebot)
*   `openai` (官方 Python SDK)
*   一个数据库后端 (代码中隐含了 `db` 对象，通常需配合 SQLite/MySQL 适配器)

## ⚠️ 注意事项

1.  **Context 长度**：代码中包含 `MAX_SAFETY_LIMIT` 和 `SUMMARY_TRIGGER_PRIVATE` 配置，请在 `config.py` 中根据你的 Token 预算合理设置，否则可能会消耗大量 Token。
2.  **安全性**：请务必保护好 `config.py` 中的 `AI_API_KEY` 和 Telegram Bot Token。
3.  **隐私**：虽然系统包含日志功能，但建议在隐私政策中告知用户会记录对话用于调试或上下文构建。

---

*Made with ❤️ by Python*
