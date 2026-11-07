# -*- coding: utf-8 -*-
import os
import re
import json
import time
import asyncio
import subprocess
import difflib
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage)
import requests

TG_API_ID = int(os.getenv("TG_API_ID"))
TG_API_HASH = os.getenv("TG_API_HASH")
TG_SESSION = os.getenv("TG_SESSION")
GEMINI_KEY_HADAF = os.getenv("GEMINI_KEY_HADAF")

GEMINI_MODEL = "gemini-flash-lite-latest"
STATE_FILE = "relay_state.json"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

CHANNELS = {
    -1004443585067: {
        "name": "اهداف المباريات",
        "target": -1001877383586,
        "source_username": "@nar0015", 
        "add_link": True,
        "link": "https://t.me/FabriAr3",
    }
}

client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: return {}
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)

state = load_state()
DUP_HISTORY_SIZE = 15
DUP_SIMILARITY_THRESHOLD = 0.82 

def is_duplicate_content(source_id, text):
    if not text or not text.strip(): return False
    key = str(source_id)
    recent = state.setdefault("recent_texts", {}).get(key, [])
    norm = re.sub(r'\s+', ' ', text).strip().lower()
    for prev in recent:
        if difflib.SequenceMatcher(None, norm, prev).ratio() >= DUP_SIMILARITY_THRESHOLD: return True
    return False

def record_published_text(source_id, text):
    if not text or not text.strip(): return
    key = str(source_id)
    norm = re.sub(r'\s+', ' ', text).strip().lower()
    bucket = state.setdefault("recent_texts", {}).setdefault(key, [])
    bucket.append(norm)
    state["recent_texts"][key] = bucket[-DUP_HISTORY_SIZE:]
    save_state(state)

def commit_state():
    try:
        subprocess.run(["git", "add", STATE_FILE], check=False)
        subprocess.run(["git", "commit", "-m", "update state"], check=False, capture_output=True)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False, capture_output=True)
        subprocess.run(["git", "push"], check=False, capture_output=True)
    except Exception: pass

def _is_small_thumbnail_photo(photo):
    try:
        dims = [(getattr(s, "w", 0), getattr(s, "h", 0)) for s in photo.sizes if hasattr(s, "w")]
        if not dims: return False
        max_w = max(w for w, _ in dims)
        max_h = max(h for _, h in dims)
        if max_w < 250 and max_h < 250: return True
        if max_w > 0 and max_h > 0:
            ratio = max_w / max_h
            if 0.85 <= ratio <= 1.18 and max(max_w, max_h) <= 800: return True
        return False
    except Exception: return False

def _is_usable_media_msg(m):
    media = m.media
    if isinstance(media, (MessageMediaPhoto, MessageMediaDocument)): return True
    if isinstance(media, MessageMediaWebPage):
        wp = getattr(media, "webpage", None)
        photo = getattr(wp, "photo", None)
        if photo and not _is_small_thumbnail_photo(photo): return True
    return False

def _media_payload(m):
    if isinstance(m.media, MessageMediaWebPage): return m.photo if m.photo else None
    return m.media

def _message_has_any_video(m):
    media = m.media
    if isinstance(media, MessageMediaDocument): return bool(m.video) or bool(getattr(m, "gif", None))
    if isinstance(media, MessageMediaWebPage):
        wp = getattr(media, "webpage", None)
        if not wp: return False
        if getattr(wp, "duration", None): return True
        wtype = str(getattr(wp, "type", "") or "").lower()
        if "video" in wtype or "gif" in wtype: return True
        doc = getattr(wp, "document", None)
        if doc and str(getattr(doc, "mime_type", "")).startswith(("video/", "image/gif")): return True
    return False

def clean_text(text):
    if not text: return ""
    cleaned = text
    # حذف روابط قناة المصدر وروابط التليجرام
    cleaned = re.sub(r'(https?://)?t\.me/\S+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'https?://t\.co/\S+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'^.*NAR.*$', '', cleaned, flags=re.MULTILINE | re.IGNORECASE)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    lines = cleaned.split('\n')
    while lines and not re.search(r'[A-Za-z\u0600-\u06FF0-9]', lines[-1]): lines.pop()
    return '\n'.join(lines).rstrip()

async def process_batch(source_id, messages):
    config = CHANNELS.get(source_id)
    if not config: return

    raw_text = next((m.message for m in messages if m.message), "")
    cleaned = clean_text(raw_text)

    # منع التكرار
    if is_duplicate_content(source_id, cleaned): return

    record_published_text(source_id, cleaned)
    final_text = cleaned

    media_msgs = [m for m in messages if _is_usable_media_msg(m)]
    force_here_we_go = False

    if any(_message_has_any_video(m) for m in messages) and not force_here_we_go:
        return # تجاهل الفيديوهات

    if config.get("add_link") and config.get("link"):
        final_text = f"{final_text}\n\n{config['link']}" if final_text else config["link"]

    try:
        if media_msgs:
            valid_payloads = [_media_payload(m) for m in media_msgs if _media_payload(m) is not None]
            if valid_payloads:
                to_send = valid_payloads[0] if len(valid_payloads) == 1 else valid_payloads
                await client.send_file(config["target"], to_send, caption=final_text or None, parse_mode="html")
                return
        if final_text:
            await client.send_message(config["target"], final_text, parse_mode="html", link_preview=False)
    except Exception as e:
        print(f"❌ خطأ: {e}")

async def catch_up_channel(source_id):
    config = CHANNELS.get(source_id, {})
    entity_ref = config.get("source_username") or source_id
    key = str(source_id)
    last_id = state.get(key, 0)

    if last_id == 0:
        latest = await client.get_messages(entity_ref, limit=1)
        if latest: last_id = max(latest[0].id - 5, 0)
        state[key] = last_id
        save_state(state)

    messages = await client.get_messages(entity_ref, min_id=last_id, limit=100)
    if not messages: return

    messages = list(reversed(messages))
    processed_ids = set()

    for msg in messages:
        if msg.id in processed_ids: continue
        if msg.grouped_id:
            group = [m for m in messages if m.grouped_id == msg.grouped_id and m.id not in processed_ids]
            group.sort(key=lambda m: m.id)
            await process_batch(source_id, group)
            processed_ids.update(m.id for m in group)
            new_last = group[-1].id
        else:
            await process_batch(source_id, [msg])
            processed_ids.add(msg.id)
            new_last = msg.id
        state[key] = new_last
        save_state(state)

async def main():
    await client.start()
    print("🚀 بدء فحص أهداف المباريات...")
    await catch_up_channel(-1004443585067)
    await client.disconnect()
    commit_state()
    print("✅ انتهت التشغيلة.")

if __name__ == "__main__":
    asyncio.run(main())
