"""
========================================
📦 项目：Telegram 自动图片/视频分发
🔖 版本：v1.1

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
   - 校验时只请求一次 Telegram，统计和发送复用消息对象
   - 支持并发发送，默认最多同时发送 2 个

5. 图片/视频处理：
   - 直接发送原图/原视频
   - 不转发原消息
   - 支持 spoiler 遮罩
   - 支持 TTL 阅后销毁
   - 图片使用固定 TTL
   - 视频按“视频时长 + 缓冲时间”动态计算 TTL，最大限制 60 秒

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
VERSION = "v1.1"
BASE_DIR = Path("/app/bot")
CONFIG_FILE = BASE_DIR / "config.json"
CACHE_FILE = BASE_DIR / "cache.json"
LOG_FILE = BASE_DIR / "bot.log"

SESSION_NAME = "user_session"

KEYWORD_PATTERN = re.compile(r"#?(j\d+)(?:\s+(\d+))?", re.IGNORECASE)
TAG_PATTERN = re.compile(r"#(\w+)", re.IGNORECASE)

UNMATCHED_REPLY_COOLDOWN = 600


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

PHOTO_TTL_SECONDS = int(config.get("photo_ttl_seconds", config.get("ttl_seconds", 10)))
VIDEO_TTL_BUFFER_SECONDS = int(config.get("video_ttl_buffer_seconds", 0))
VIDEO_TTL_MAX_SECONDS = int(config.get("video_ttl_max_seconds", 60))

SEND_DELAY = float(config.get("send_delay", 0.3))
MAX_SEND = int(config.get("max_send", 5))
MAX_CONCURRENT_SENDS = int(config.get("max_concurrent_sends", 3))


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


def get_video_duration(msg):
    if not msg or not msg.video:
        return None

    for attr in getattr(msg.video, "attributes", []):
        duration = getattr(attr, "duration", None)
        if duration:
            return int(duration)

    return None


def get_media_ttl(msg):
    if msg.photo:
        return PHOTO_TTL_SECONDS, "图片", None

    if msg.video:
        duration = get_video_duration(msg)

        if duration:
            ttl = min(duration + VIDEO_TTL_BUFFER_SECONDS, VIDEO_TTL_MAX_SECONDS)
            return ttl, "视频", duration

        return VIDEO_TTL_MAX_SECONDS, "视频", None

    return PHOTO_TTL_SECONDS, "未知", None


def count_media_types(messages):
    photo_count = sum(1 for msg in messages if msg.photo)
    video_count = sum(1 for msg in messages if msg.video)
    return photo_count, video_count


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
    valid_msgs = []

    for mid in GROUPS.get(key, []):
        try:
            msg = await client.get_messages(CHANNEL_ID, ids=mid)

            if is_media(msg):
                valid_ids.append(mid)
                valid_msgs.append(msg)
            else:
                logging.warning(f"⚠️ 媒体无效，已移除：分组={key}，消息ID={mid}")

        except Exception as e:
            logging.warning(f"⚠️ 校验媒体异常，已跳过：分组={key}，消息ID={mid}，错误={e}")

    if valid_ids:
        GROUPS[key] = valid_ids
    else:
        GROUPS.pop(key, None)

    save_cache()
    return valid_ids, valid_msgs


# =========================
# 📤 发送媒体
# =========================
async def send_media(client, chat_id, msg):
    try:
        ttl_seconds, media_type, duration = get_media_ttl(msg)

        if duration is not None:
            logging.info(
                f"⏱️ 视频动态TTL：视频时长={duration}秒，"
                f"缓冲={VIDEO_TTL_BUFFER_SECONDS}秒，最终TTL={ttl_seconds}秒，消息ID：{msg.id}"
            )
        else:
            logging.info(f"⏱️ {media_type}TTL：{ttl_seconds}秒，消息ID：{msg.id}")

        logging.info(f"📥 开始下载{media_type}，消息ID：{msg.id}")

        file_bytes = await msg.download_media(bytes)

        if not file_bytes:
            logging.warning(f"⚠️ 媒体下载失败，内容为空，消息ID：{msg.id}")
            return False

        bio = io.BytesIO(file_bytes)

        if msg.photo:
            bio.name = "photo.jpg"
        elif msg.video:
            bio.name = "video.mp4"
        else:
            logging.warning(f"⚠️ 不支持的媒体类型，消息ID：{msg.id}")
            return False

        logging.info(
            f"📤 开始发送{media_type}，消息ID：{msg.id}，"
            f"大小：{len(file_bytes)} 字节，TTL={ttl_seconds}秒"
        )

        await client.send_file(
            chat_id,
            bio,
            force_document=False,
            spoiler=True,
            ttl=ttl_seconds
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
# 🚀 并发发送处理
# =========================
async def send_media_concurrently(client, event, send_msgs, user_log, key):
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SENDS)

    async def send_one(msg):
        async with semaphore:
            try:
                ok = await send_media(client, event.chat_id, msg)

                # 图片保留发送间隔；视频不额外等待，提高发送速度
                if msg.photo and SEND_DELAY > 0:
                    await asyncio.sleep(SEND_DELAY)

                return ok

            except Exception as e:
                logging.error(
                    f"❌ 处理媒体异常：用户={user_log}，分组={key}，"
                    f"消息ID={getattr(msg, 'id', '未知')}，错误={e}"
                )
                return False

    results = await asyncio.gather(
        *(send_one(msg) for msg in send_msgs),
        return_exceptions=True
    )

    return sum(1 for result in results if result is True)


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

    valid_ids, valid_msgs = await validate_group_media(client, key)

    if not valid_msgs:
        await event.reply("该分组暂无有效图片或视频")
        logging.warning(f"⚠️ 分组无有效媒体：用户={user_log}，分组={key}")
        return

    send_msgs = valid_msgs[:count] if count else valid_msgs[:MAX_SEND]
    send_ids = [msg.id for msg in send_msgs]

    photo_count, video_count = count_media_types(send_msgs)

    await event.reply(
        f"✅ 匹配到 {key.upper()}，准备发送：\n"
        f"🖼 图片 {photo_count} 张\n"
        f"🎬 视频 {video_count} 个\n"
        f"📦 总计 {len(send_msgs)} 个\n"
        "\n"
        "Tips：⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️\n"
        "视频文件较大，发送时间过长，请耐心等待"
    )

    logging.info(
        f"✅ 用户关键词匹配成功：用户={user_log}，输入={text}，分组={key}，"
        f"分组总数={len(valid_msgs)}，本次发送={len(send_msgs)}，"
        f"图片={photo_count}，视频={video_count}，消息ID={send_ids}，"
        f"并发数={MAX_CONCURRENT_SENDS}"
    )

    success_count = await send_media_concurrently(
        client=client,
        event=event,
        send_msgs=send_msgs,
        user_log=user_log,
        key=key
    )

    logging.info(
        f"📊 用户请求处理完成：用户={user_log}，分组={key}，"
        f"计划发送={len(send_msgs)}，成功发送={success_count}"
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
    logging.info(
        f"⚙️ 当前配置：图片TTL={PHOTO_TTL_SECONDS}秒，"
        f"视频TTL=视频时长+{VIDEO_TTL_BUFFER_SECONDS}秒，最大{VIDEO_TTL_MAX_SECONDS}秒，"
        f"发送间隔={SEND_DELAY}秒，默认最多发送={MAX_SEND}个，"
        f"并发发送数={MAX_CONCURRENT_SENDS}"
    )
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
