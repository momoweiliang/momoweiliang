"""
========================================
📦 项目：Telegram 自动图片/视频分发
🔖 版本：v1.0

✅ 已实现功能：
1. 关键词严格匹配：
   - 支持：J1 / J1 3 / #J1 / #J1 3
   - 不支持模糊匹配

2. 自动分组系统：
   - 通过频道标签（#J1、#J2 等）自动归类
   - 标签消息本身带图片/视频，也会自动加入对应分组
   - 标签后的图片/视频自动归入当前分组

3. 热加载：
   - 新增标签/图片/视频实时生效
   - 删除消息自动同步分组
   - 无需重启程序

4. 智能发送：
   - 支持数量控制，例如：J1 3 / #J1 3
   - 不指定数量时，默认最多发送 MAX_SEND 个
   - 发送前自动校验媒体是否仍然有效

5. 图片/视频处理：
   - 直接发送原图/原视频
   - 不转发原消息
   - 支持 spoiler 遮罩
   - 支持 TTL 阅后销毁

6. 中文日志系统：
   - 全中文业务日志
   - 屏蔽 Telethon 内部英文 INFO 日志
   - 记录用户请求、匹配结果、发送结果、错误信息

7. 未匹配保护：
   - 未匹配关键词时发送提示
   - 同一用户 10 分钟内只提示一次，防刷屏

8. 缓存机制：
   - 自动保存分组状态
   - 启动时从频道现存消息重建，避免旧缓存脏数据

========================================
"""

import re
import json
import io
import time
import asyncio
import logging
from collections import defaultdict
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import TtlMediaInvalidError


# =========================
# 🔧 基础常量
# =========================
VERSION = "v1.0"
BASE_DIR = Path("/app/bot")
CONFIG_FILE = BASE_DIR / "config.json"
CACHE_FILE = BASE_DIR / "cache.json"
LOG_FILE = BASE_DIR / "bot.log"

SESSION_NAME = "user_session"

KEYWORD_PATTERN = re.compile(r"#?(j\d+)(?:\s+(\d+))?", re.IGNORECASE)
TAG_PATTERN = re.compile(r"#(\w+)", re.IGNORECASE)

UNMATCHED_REPLY_COOLDOWN = 600  # 10分钟


# =========================
# 📝 日志配置
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    force=True,
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

logging.getLogger("telethon").setLevel(logging.WARNING)


# =========================
# 🔧 配置读取
# =========================
with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

API_ID = config["api_id"]
API_HASH = config["api_hash"]
CHANNEL_ID = config["channel_id"]

TTL_SECONDS = int(config.get("ttl_seconds", 10))
SEND_DELAY = float(config.get("send_delay", 0.3))
MAX_SEND = int(config.get("max_send", 5))


# =========================
# 🧠 全局数据
# =========================
GROUPS = defaultdict(list)
LAST_MSG_ID = 0
CURRENT_TAG = None
LAST_UNMATCHED_REPLY = {}


# =========================
# 🔧 工具函数
# =========================
def get_text(msg):
    return getattr(msg, "message", None) or ""


def extract_tags(text):
    if not text:
        return []
    return [tag.lower() for tag in TAG_PATTERN.findall(text)]


def is_media(msg):
    return bool(msg and (msg.photo or msg.video))


def parse_keyword(text):
    text = (text or "").strip().lower()
    m = KEYWORD_PATTERN.fullmatch(text)

    if not m:
        return None, None

    key = m.group(1).lower()
    count = int(m.group(2)) if m.group(2) else None

    return key, count


def match_group(text):
    key, _ = parse_keyword(text)
    return key if key in GROUPS else None


def parse_count(text):
    _, count = parse_keyword(text)
    return count


def add_media_to_group(tag, msg_id):
    if not tag:
        return False

    if msg_id not in GROUPS[tag]:
        GROUPS[tag].append(msg_id)
        return True

    return False


def normalize_groups():
    for key in list(GROUPS.keys()):
        GROUPS[key] = sorted(list(dict.fromkeys(GROUPS[key])))

        if not GROUPS[key]:
            del GROUPS[key]


def group_summary():
    return {key: len(value) for key, value in GROUPS.items()}


def save_cache():
    normalize_groups()

    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": VERSION,
                    "groups": dict(GROUPS),
                    "last_msg_id": LAST_MSG_ID,
                    "current_tag": CURRENT_TAG
                },
                f,
                ensure_ascii=False,
                indent=2
            )
    except Exception as e:
        logging.error(f"❌ 缓存保存失败：{e}")


def get_user_log_name(user):
    if not user:
        return "未知用户"

    username = f"@{user.username}" if getattr(user, "username", None) else "无用户名"
    first_name = getattr(user, "first_name", "") or ""
    last_name = getattr(user, "last_name", "") or ""
    name = (first_name + " " + last_name).strip() or "无昵称"

    return f"{name} / {username} / ID:{user.id}"


# =========================
# 📡 启动时重建频道分组
# =========================
async def rebuild_groups_from_channel(client):
    global GROUPS, LAST_MSG_ID, CURRENT_TAG

    GROUPS = defaultdict(list)
    LAST_MSG_ID = 0
    CURRENT_TAG = None

    logging.info("📡 开始从频道现存消息重建分组")

    async for msg in client.iter_messages(CHANNEL_ID, reverse=True):

        if msg.__class__.__name__ == "MessageService":
            continue

        text = get_text(msg)
        tags = extract_tags(text)

        if tags:
            CURRENT_TAG = tags[0]
            logging.info(f"🏷️ 扫描到标签：{CURRENT_TAG}，消息ID：{msg.id}")

            if is_media(msg):
                added = add_media_to_group(CURRENT_TAG, msg.id)
                if added:
                    logging.info(f"🖼️ 标签消息自带媒体，已加入分组：{CURRENT_TAG}，消息ID：{msg.id}")

            LAST_MSG_ID = max(LAST_MSG_ID, msg.id)
            continue

        if is_media(msg) and CURRENT_TAG:
            added = add_media_to_group(CURRENT_TAG, msg.id)
            if added:
                logging.info(f"🖼️ 扫描到媒体：消息ID {msg.id} -> 分组 {CURRENT_TAG}")

        LAST_MSG_ID = max(LAST_MSG_ID, msg.id)

    normalize_groups()
    save_cache()

    logging.info(f"✅ 分组重建完成，当前分组：{group_summary()}")


# =========================
# ✅ 发送前校验媒体
# =========================
async def validate_group_media(client, key):
    valid_ids = []

    for mid in GROUPS.get(key, []):
        try:
            msg = await client.get_messages(CHANNEL_ID, ids=mid)

            if is_media(msg):
                valid_ids.append(mid)
            else:
                logging.warning(f"⚠️ 媒体无效，已移除：分组={key}，消息ID={mid}")

        except Exception as e:
            logging.warning(f"⚠️ 校验媒体异常，已跳过：分组={key}，消息ID={mid}，错误={e}")

    if valid_ids:
        GROUPS[key] = valid_ids
    else:
        GROUPS.pop(key, None)

    save_cache()
    return valid_ids


# =========================
# 📤 发送媒体
# =========================
async def send_media(client, chat_id, msg):
    try:
        media_type = "图片" if msg.photo else "视频"
        logging.info(f"📥 开始下载{media_type}，消息ID：{msg.id}")

        file_bytes = await msg.download_media(bytes)

        if not file_bytes:
            logging.warning(f"⚠️ 媒体下载失败，内容为空，消息ID：{msg.id}")
            return False

        bio = io.BytesIO(file_bytes)
        bio.name = "photo.jpg" if msg.photo else "video.mp4"

        logging.info(f"📤 开始发送{media_type}，消息ID：{msg.id}，大小：{len(file_bytes)} 字节")

        await client.send_file(
            chat_id,
            bio,
            force_document=False,
            spoiler=True,
            ttl=TTL_SECONDS
        )

        logging.info(f"✅ {media_type}发送成功，消息ID：{msg.id}")
        return True

    except TtlMediaInvalidError:
        logging.warning(f"⚠️ 当前媒体不支持 TTL 阅后销毁，已跳过，消息ID：{getattr(msg, 'id', '未知')}")
        return False

    except Exception as e:
        logging.error(f"❌ 媒体发送失败，消息ID：{getattr(msg, 'id', '未知')}，错误：{e}")
        return False


# =========================
# 🔥 频道热加载处理
# =========================
async def handle_channel_message(event):
    global CURRENT_TAG, LAST_MSG_ID

    msg = event.message

    if msg.__class__.__name__ == "MessageService":
        return

    text = get_text(msg)
    tags = extract_tags(text)

    if tags:
        CURRENT_TAG = tags[0]
        logging.info(f"🏷️ 当前分组切换为：{CURRENT_TAG}，消息ID：{msg.id}")

        if is_media(msg):
            added = add_media_to_group(CURRENT_TAG, msg.id)
            if added:
                logging.info(f"✅ 新标签媒体已加入分组：{CURRENT_TAG}，消息ID：{msg.id}")

        LAST_MSG_ID = max(LAST_MSG_ID, msg.id)
        save_cache()

        logging.info(f"📦 当前分组状态：{group_summary()}")
        return

    if is_media(msg) and CURRENT_TAG:
        added = add_media_to_group(CURRENT_TAG, msg.id)

        LAST_MSG_ID = max(LAST_MSG_ID, msg.id)
        save_cache()

        if added:
            logging.info(f"✅ 新媒体已加入分组：{CURRENT_TAG}，消息ID：{msg.id}")
            logging.info(f"📦 当前分组状态：{group_summary()}")


# =========================
# 🗑️ 频道删除同步处理
# =========================
async def handle_deleted_messages(event):
    deleted_ids = set(event.deleted_ids)
    changed = False

    logging.info(f"🗑️ 检测到频道消息删除：{list(deleted_ids)}")

    for key in list(GROUPS.keys()):
        old_count = len(GROUPS[key])
        GROUPS[key] = [mid for mid in GROUPS[key] if mid not in deleted_ids]

        new_count = len(GROUPS.get(key, []))

        if new_count != old_count:
            changed = True
            logging.info(f"🗑️ 已从分组 {key} 移除被删除媒体，原数量 {old_count}，现数量 {new_count}")

        if key in GROUPS and not GROUPS[key]:
            del GROUPS[key]
            logging.info(f"🗑️ 分组 {key} 已清空并移除")

    if changed:
        save_cache()
        logging.info(f"📦 当前分组状态：{group_summary()}")


# =========================
# 💬 私聊关键词处理
# =========================
async def handle_private_message(client, event):
    if not event.is_private:
        return

    text = event.raw_text or ""
    user = await event.get_sender()
    user_log = get_user_log_name(user)

    logging.info(f"👤 收到用户消息：用户={user_log}，内容={text}")

    key, count = parse_keyword(text)

    if not key or key not in GROUPS:
        user_id = event.sender_id
        now = time.time()
        last_time = LAST_UNMATCHED_REPLY.get(user_id, 0)

        logging.info(f"❌ 用户关键词未匹配：用户={user_log}，内容={text}")

        if now - last_time >= UNMATCHED_REPLY_COOLDOWN:
            await event.reply(
                "❌未匹配到关键词（示例：J1 或 J1 3 或 #J1 或 #J1 3），10分钟内不再发送此消息！！！"
            )
            LAST_UNMATCHED_REPLY[user_id] = now
            logging.info(f"📩 已向用户发送未匹配提示：用户={user_log}")
        else:
            logging.info(f"⏳ 未匹配提示处于冷却中，未重复发送：用户={user_log}")

        return

    valid_msgs = await validate_group_media(client, key)

    if not valid_msgs:
        await event.reply("该分组暂无有效图片或视频")
        logging.warning(f"⚠️ 分组无有效媒体：用户={user_log}，分组={key}")
        return

    send_ids = valid_msgs[:count] if count else valid_msgs[:MAX_SEND]

    await event.reply(f"✅ 匹配到 {key.upper()}，准备发送 {len(send_ids)} 个图片/视频")

    logging.info(
        f"✅ 用户关键词匹配成功：用户={user_log}，输入={text}，分组={key}，"
        f"分组总数={len(valid_msgs)}，本次发送={len(send_ids)}，消息ID={send_ids}"
    )

    success_count = 0

    for mid in send_ids:
        try:
            msg = await client.get_messages(CHANNEL_ID, ids=mid)

            if not is_media(msg):
                logging.warning(f"⚠️ 发送前发现无效媒体，已跳过：分组={key}，消息ID={mid}")
                continue

            ok = await send_media(client, event.chat_id, msg)

            if ok:
                success_count += 1

            await asyncio.sleep(SEND_DELAY)

        except Exception as e:
            logging.error(f"❌ 处理媒体异常：用户={user_log}，分组={key}，消息ID={mid}，错误={e}")

    logging.info(
        f"📊 用户请求处理完成：用户={user_log}，分组={key}，"
        f"计划发送={len(send_ids)}，成功发送={success_count}"
    )

    if success_count == 0:
        await event.reply("⚠️ 匹配到了分组，但没有成功发送媒体，请检查图片/视频是否支持 TTL")
        logging.warning(f"⚠️ 用户请求没有成功发送任何媒体：用户={user_log}，分组={key}")


# =========================
# 🚀 主程序
# =========================
async def main():
    client = TelegramClient(str(BASE_DIR / SESSION_NAME), API_ID, API_HASH)
    await client.start()

    logging.info(f"🚀 系统启动成功，当前版本：{VERSION}")
    logging.info(f"⚙️ 当前配置：TTL={TTL_SECONDS}秒，发送间隔={SEND_DELAY}秒，默认最多发送={MAX_SEND}个")
    logging.info(f"📁 配置文件：{CONFIG_FILE}")
    logging.info(f"📁 缓存文件：{CACHE_FILE}")
    logging.info(f"📁 日志文件：{LOG_FILE}")

    await rebuild_groups_from_channel(client)

    @client.on(events.NewMessage(chats=CHANNEL_ID))
    async def channel_watcher(event):
        await handle_channel_message(event)

    @client.on(events.MessageDeleted(chats=CHANNEL_ID))
    async def delete_watcher(event):
        await handle_deleted_messages(event)

    @client.on(events.NewMessage(incoming=True))
    async def private_handler(event):
        await handle_private_message(client, event)

    logging.info("✅ 系统已进入监听状态，等待用户消息和频道更新")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🛑 收到停止信号，系统已退出")
    except Exception as e:
        logging.exception(f"❌ 系统异常退出：{e}")
