from __future__ import annotations
import asyncio, os, time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from bot.database import DraftDatabase
from bot.source_normalization import normalize_source_url, normalize_telegram_channel_input
from bot.sources import COMMUNITY_RSS, OFFICIAL_AI_RSS, RU_TECH_RSS, TECH_MEDIA_RSS, TOOLS_RSS, discover_rss_feed_url, get_builtin_source_override, parse_custom_topic_feeds, reddit_sources_enabled, x_sources_enabled


def is_valid_rss_input_url(raw: str) -> bool:
    return (raw or "").strip().startswith("http")

def built_in_rss_sources() -> list[dict]:
    groups=[("official_ai",OFFICIAL_AI_RSS,True),("tech_media",TECH_MEDIA_RSS,True),("ru_tech",RU_TECH_RSS,True),("tools",TOOLS_RSS,True),("community",COMMUNITY_RSS,reddit_sources_enabled())]
    rows=[]
    for group,items,enabled in groups:
        for name,url in items:
            override=get_builtin_source_override("rss",url); status="enabled" if enabled else "skipped"; reason=""
            if override and override.get("action")=="disable": status="disabled"; reason=override.get("reason","")
            rows.append({"status":status,"reason":reason,"source_type":"rss","group":group,"name":name,"value":url,"normalized_value":normalize_source_url(url),"location":"built-in"})
    return rows

def env_configured_sources(settings)->list[dict]:
    rows=[]
    for name,group,url in parse_custom_topic_feeds(os.getenv("CUSTOM_TOPIC_FEEDS")):
        rows.append({"status":"enabled","source_type":"rss","group":group,"name":name,"value":url,"normalized_value":normalize_source_url(url),"location":"env"})
    for channel in list(getattr(settings,"telegram_source_channels",[]) or []):
        username=normalize_telegram_channel_input(channel)
        if username: rows.append({"status":"enabled","source_type":"telegram","group":"telegram","name":f"Telegram @{username}","value":username,"normalized_value":username.lower(),"location":"env"})
    if x_sources_enabled():
        for account in os.getenv("X_ACCOUNTS","").split(","):
            username=account.strip().lstrip("@")
            if username: rows.append({"status":"enabled","source_type":"x","group":"x","name":f"X @{username}","value":username,"normalized_value":username.lower(),"location":"env"})
    return rows

def db_managed_sources(db: DraftDatabase)->list[dict]:
    rows=[]
    for row in db.list_managed_sources(include_disabled=True):
        st=str(row["source_type"] or "").strip().lower(); v=str(row["value"] or "").strip()
        rows.append({"status":"enabled" if bool(row["enabled"]) else "disabled","source_type":st,"group":str(row["source_group"] or "custom"),"name":str(row["name"] or "-"),"value":v,"normalized_value":normalize_source_url(v) if st=="rss" else v.lower(),"location":"my sources"})
    return rows

def find_duplicate_source(source_type:str,value:str,settings,db:DraftDatabase)->dict|None:
    normalized=normalize_source_url(value) if source_type=="rss" else (normalize_telegram_channel_input(value).lower() if source_type=="telegram" else (value or "").strip().lower())
    if not normalized: return None
    for item in [*built_in_rss_sources(),*env_configured_sources(settings),*db_managed_sources(db)]:
        if item["source_type"]==source_type and item.get("normalized_value")==normalized: return item
    return None

def sources_hub_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Список источников",callback_data="sources_list")],[InlineKeyboardButton("📚 Все источники",callback_data="sources_inventory")],[InlineKeyboardButton("➕ Добавить RSS",callback_data="source_add_rss")],[InlineKeyboardButton("➕ Добавить Telegram-канал",callback_data="source_add_telegram")],[InlineKeyboardButton("🧪 Проверить источники",callback_data="menu_sources_status")],[InlineKeyboardButton("🩺 Здоровье источников",callback_data="sources_health")],[InlineKeyboardButton("⬅️ Назад",callback_data="menu_back")]])

def source_card_keyboard(source_id:int,enabled:bool):
    toggle="⛔ Disable" if enabled else "✅ Enable"
    return InlineKeyboardMarkup([[InlineKeyboardButton(toggle,callback_data=f"source_toggle:{source_id}"),InlineKeyboardButton("🧪 Test",callback_data=f"source_test:{source_id}"),InlineKeyboardButton("🗑 Delete",callback_data=f"source_delete:{source_id}")]])

def render_sources_status(reports, db, source_group_labels):
    total=len(reports); ok=sum(1 for r in reports if r.status=="ok"); empty=sum(1 for r in reports if r.status=="empty"); skipped=sum(1 for r in reports if r.status=="skipped"); errors=sum(1 for r in reports if r.status=="error")
    lines=["📡 Статус источников","Чтобы посмотреть полный список источников: 📡 Источники → 📚 Все источники","",f"Всего источников: {total}",f"Работают: {ok}",f"Пустые: {empty}",f"Отключены/пропущены: {skipped}",f"Ошибки: {errors}","","По группам:"]
    for group,label in source_group_labels.items():
        grp=[r for r in reports if (r.source_group or "other")==group]
        if not grp:
            if group=="custom": lines.append(f"{label}: 0/0")
            continue
        gok=sum(1 for r in grp if r.status=="ok"); gskip=sum(1 for r in grp if r.status=="skipped"); suffix=f", пропущено {gskip}" if gskip else ""; lines.append(f"{label}: {gok}/{len(grp)}{suffix}")
    probs=[r for r in reports if r.status in {"error","empty"}]
    if probs:
        lines += ["","Проблемы:"]
        for rep in probs[:12]: lines.append(f"- {rep.name}: {rep.error or 'ошибка'}" if rep.status=="error" else f"- {rep.name}: 0 тем")
    if db is not None:
        health_rows=db.list_source_health(limit=500)
        if not health_rows: lines.append("\nИстория здоровья пока пустая. Запусти /collect или проверку источников.")
    return "\n".join(lines)[:3900]

def render_sources_health(db:DraftDatabase)->str:
    rows=db.list_source_health(limit=200)
    if not rows: return "История здоровья пока пустая. Запусти /collect или проверку источников."
    parts=["🩺 Здоровье источников"]
    groups=[("✅ Работают", lambda r:r["last_status"]=="ok"),("⚠️ Пустые", lambda r:r["last_status"]=="empty"),("❌ Ошибки", lambda r:r["last_status"]=="error"),("⏸ На паузе", lambda r:bool(r["disabled_until"]))]
    for label,fn in groups:
        selected=[r for r in rows if fn(r)]
        if not selected: continue
        parts.append(f"\n{label}:")
        for r in selected[:20]:
            line=f"[{r['source_type']}/{r['source_group']}] {r['source_name']}"
            if r["last_status"]=="error": line += f" - {str(r['last_error'] or 'ошибка')[:80]}, ошибок подряд: {int(r['consecutive_errors'] or 0)}"
            if r["disabled_until"]: line += f" - пауза до {str(r['disabled_until'])[11:16]}"
            parts.append(line)
    text="\n".join(parts)
    for secret in ["BOT_TOKEN", "TELEGRAM_SESSION_STRING", "TELEGRAM_API_HASH", "OPENROUTER_API_KEY", "OPENAI_API_KEY"]:
        text = text.replace(secret, "***")
    return text[:3900]

def render_sources_inventory(settings,db,detect_railway):
    builtin=built_in_rss_sources(); env_rows=env_configured_sources(settings); managed=db_managed_sources(db)
    all_rows=[("Встроенные RSS",builtin),("Env источники",env_rows),("Мои источники (DB)",managed)]
    messages=[f"Всего источников: {len(builtin)+len(env_rows)+len(managed)}. Встроенные: {len(builtin)}. Env: {len(env_rows)}. Мои: {len(managed)}."]
    if detect_railway(settings.db_path): messages.append("⚠️ DB_PATH сейчас local data/drafts.db. Источники, добавленные через бота, могут пропасть после redeploy. Лучше подключить Railway Volume и поставить DB_PATH=/data/drafts.db.")
    current=""; icon_map={"enabled":"✅","skipped":"⏭️","disabled":"⛔"}
    for title,rows in all_rows:
        bl=[f"\n{title}:"]
        if not rows: bl.append("— пусто")
        else:
            for item in rows:
                icon=icon_map.get(item.get("status","enabled"),"•"); reason=str(item.get("reason") or "").strip(); suffix=f" ({reason})" if reason else ""
                bl.append(f"{icon} [{item.get('source_type')}/{item.get('group')}] {item.get('name')} — {item.get('value')}{suffix}")
        block="\n".join(bl)
        if len(current)+len(block)+1>3500: messages.append(current.strip()); current=block
        else: current += ("\n"+block) if current else block
    if current.strip(): messages.append(current.strip())
    return messages

async def run_sources_status_background(context, settings, db, run_collect, render_status, back_to_menu_keyboard, logger):
    started=time.perf_counter()
    try:
        _items,reports=await run_collect(settings=settings,db=db)
        await context.bot.send_message(chat_id=settings.admin_id,text=render_status(reports,db),reply_markup=back_to_menu_keyboard())
    except Exception:
        logger.exception("Background source diagnostics failed"); await context.bot.send_message(chat_id=settings.admin_id,text="Не удалось проверить источники. Попробуй ещё раз.")
    finally:
        logger.info("Source diagnostics completed in %.2fs", time.perf_counter()-started); context.application.bot_data["sources_check_running"]=False

async def run_source_test_background(context, settings, db, row, run_collect, logger):
    sid=int(row["id"]); started=time.perf_counter()
    try:
        if row["source_type"]=="rss":
            feed_url,error=await asyncio.to_thread(discover_rss_feed_url,str(row["value"]))
            await context.bot.send_message(chat_id=settings.admin_id,text=f"RSS test #{sid}: OK: {feed_url}" if feed_url else f"RSS test #{sid}: Ошибка: {error}"); return
        _items,reports=await run_collect(settings=settings,db=db); matched=[r for r in reports if str(r.url).endswith(str(row["value"])) or r.name==f"Telegram @{row['value']}"]
        report=matched[0] if matched else None
        await context.bot.send_message(chat_id=settings.admin_id,text=f"Telegram test #{sid}: {report.status if report else 'нет данных'}")
    except Exception:
        logger.exception("Background source test failed for source_id=%s",sid); await context.bot.send_message(chat_id=settings.admin_id,text=f"Не удалось проверить источник #{sid}.")
    finally:
        logger.info("Source test completed for source_id=%s in %.2fs",sid,time.perf_counter()-started)
        running=context.application.bot_data.get("source_test_running")
        if isinstance(running,set): running.discard(sid)

async def handle_sources_callback(update, context, data, edit_callback_message, sources_hub_keyboard, source_card_keyboard, run_source_test_background, render_sources_inventory):
    settings=context.bot_data["settings"]; db=context.bot_data["db"]; query=update.callback_query
    if not query: return
    # (same logic)
    if data=="sources_list":
        rows=db.list_managed_sources(include_disabled=True)
        if not rows: return await edit_callback_message(query,"Пока нет пользовательских источников.",reply_markup=sources_hub_keyboard())
        await edit_callback_message(query,f"Источников: {len(rows)}",reply_markup=sources_hub_keyboard())
        for row in rows:
            enabled=bool(row["enabled"]); status="✅" if enabled else "⛔"; text=f"#{row['id']} {status} [{row['source_type']}]\n{row['name']}\n{row['value']}\nstatus: {row['last_status'] or '-'}"
            if row["last_error"]: text += f"\nerr: {str(row['last_error'])[:120]}"
            await context.bot.send_message(chat_id=settings.admin_id,text=text,reply_markup=source_card_keyboard(int(row["id"]),enabled))
        return
    if data=="sources_inventory":
        parts=render_sources_inventory(settings,db); await edit_callback_message(query,parts[0],reply_markup=sources_hub_keyboard())
        for part in parts[1:]: await context.bot.send_message(chat_id=settings.admin_id,text=part,reply_markup=sources_hub_keyboard()); return
    if data=="source_add_rss": context.user_data["source_add_flow"]={"type":"rss","step":"name"}; return await edit_callback_message(query,"Введите название RSS-источника.",reply_markup=sources_hub_keyboard())
    if data=="source_add_telegram": context.user_data["source_add_flow"]={"type":"telegram","step":"value"}; return await edit_callback_message(query,"Пришлите username канала или ссылку t.me/...",reply_markup=sources_hub_keyboard())
    if data=="source_confirm_rss":
        flow=context.user_data.get("source_add_flow") or {}
        if flow.get("type")=="rss" and flow.get("step")=="confirm" and flow.get("feed_url"):
            try: db.create_managed_source("rss",str(flow.get("name") or "RSS"),str(flow["feed_url"]),"custom")
            except ValueError as exc: return await edit_callback_message(query,f"Не удалось добавить источник: {exc}",reply_markup=sources_hub_keyboard())
            context.user_data.pop("source_add_flow",None); return await edit_callback_message(query,"RSS-источник добавлен.",reply_markup=sources_hub_keyboard())
        return await edit_callback_message(query,"Нет данных для сохранения.",reply_markup=sources_hub_keyboard())
    if data=="source_cancel_add": context.user_data.pop("source_add_flow",None); return await edit_callback_message(query,"Добавление отменено.",reply_markup=sources_hub_keyboard())
    if data.startswith("source_toggle:"): sid=int(data.split(":",1)[1]); row=db.get_managed_source(sid); row and db.update_managed_source_enabled(sid,not bool(row["enabled"])); return await edit_callback_message(query,"Обновил статус источника.",reply_markup=sources_hub_keyboard())
    if data.startswith("source_delete:"): db.delete_managed_source(int(data.split(":",1)[1])); return await edit_callback_message(query,"Источник удалён.",reply_markup=sources_hub_keyboard())
    if data.startswith("source_test:"):
        sid=int(data.split(":",1)[1]); row=db.get_managed_source(sid)
        if not row: return await edit_callback_message(query,"Источник не найден.",reply_markup=sources_hub_keyboard())
        if context.application.bot_data.get("sources_check_running"): return await edit_callback_message(query,"Проверка источников уже идёт. Дождись результата.",reply_markup=sources_hub_keyboard())
        running=context.application.bot_data.setdefault("source_test_running",set())
        if sid in running: return await edit_callback_message(query,"Проверка этого источника уже идёт. Дождись результата.",reply_markup=sources_hub_keyboard())
        running.add(sid); await edit_callback_message(query,"Проверяю RSS..." if row["source_type"]=="rss" else "Проверяю источник...",reply_markup=sources_hub_keyboard()); context.application.create_task(run_source_test_background(context=context,settings=settings,db=db,row=row)); return
    await edit_callback_message(query,"Неизвестное действие источников.",reply_markup=sources_hub_keyboard())
