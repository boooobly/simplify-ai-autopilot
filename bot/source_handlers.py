from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.database import DraftDatabase
from bot.source_normalization import normalize_source_url, normalize_telegram_channel_input
from bot.sources import (
    COMMUNITY_RSS,
    OFFICIAL_AI_RSS,
    RU_TECH_RSS,
    TECH_MEDIA_RSS,
    TOOLS_RSS,
    discover_rss_feed_url,
    get_builtin_source_override,
    parse_custom_topic_feeds,
    reddit_sources_enabled,
    x_sources_enabled,
)


def is_valid_rss_input_url(raw: str) -> bool:
    parsed = urlparse((raw or "").strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def built_in_rss_sources(settings=None) -> list[dict]:
    groups = [
        ("official_ai", OFFICIAL_AI_RSS, True),
        ("tech_media", TECH_MEDIA_RSS, True),
        ("ru_tech", RU_TECH_RSS, True),
        ("tools", TOOLS_RSS, True),
        ("community", COMMUNITY_RSS, reddit_sources_enabled(settings)),
    ]
    rows: list[dict] = []
    for group, items, enabled in groups:
        for name, url in items:
            override = get_builtin_source_override("rss", url)
            status = "enabled" if enabled else "skipped"
            reason = ""
            if override and override.get("action") == "disable":
                status = "disabled"
                reason = override.get("reason", "")
            rows.append(
                {
                    "status": status,
                    "reason": reason,
                    "source_type": "rss",
                    "group": group,
                    "name": name,
                    "value": url,
                    "normalized_value": normalize_source_url(url),
                    "location": "built-in",
                }
            )
    return rows


def env_configured_sources(settings) -> list[dict]:
    rows: list[dict] = []
    for name, group, url in parse_custom_topic_feeds(os.getenv("CUSTOM_TOPIC_FEEDS")):
        rows.append(
            {
                "status": "enabled",
                "source_type": "rss",
                "group": group,
                "name": name,
                "value": url,
                "normalized_value": normalize_source_url(url),
                "location": "env",
            }
        )
    for channel in list(getattr(settings, "telegram_source_channels", []) or []):
        username = normalize_telegram_channel_input(channel)
        if username:
            rows.append(
                {
                    "status": "enabled",
                    "source_type": "telegram",
                    "group": "telegram",
                    "name": f"Telegram @{username}",
                    "value": username,
                    "normalized_value": username.lower(),
                    "location": "env",
                }
            )
    if x_sources_enabled(settings):
        for account in list(getattr(settings, "x_accounts", []) or []):
            username = str(account).strip().lstrip("@")
            if username:
                rows.append(
                    {
                        "status": "enabled",
                        "source_type": "x",
                        "group": "x",
                        "name": f"X @{username}",
                        "value": username,
                        "normalized_value": username.lower(),
                        "location": "env",
                    }
                )
    return rows


def db_managed_sources(db: DraftDatabase) -> list[dict]:
    rows: list[dict] = []
    for row in db.list_managed_sources(include_disabled=True):
        source_type = str(row["source_type"] or "").strip().lower()
        value = str(row["value"] or "").strip()
        rows.append(
            {
                "status": "enabled" if bool(row["enabled"]) else "disabled",
                "source_type": source_type,
                "group": str(row["source_group"] or "custom"),
                "name": str(row["name"] or "-"),
                "value": value,
                "normalized_value": normalize_source_url(value) if source_type == "rss" else value.lower(),
                "location": "my sources",
            }
        )
    return rows


def find_duplicate_source(source_type: str, value: str, settings, db: DraftDatabase) -> dict | None:
    if source_type == "rss":
        normalized = normalize_source_url(value)
    elif source_type == "telegram":
        normalized = normalize_telegram_channel_input(value).lower()
    else:
        normalized = (value or "").strip().lower()
    if not normalized:
        return None
    inventory = [*built_in_rss_sources(settings), *env_configured_sources(settings), *db_managed_sources(db)]
    for item in inventory:
        if item["source_type"] == source_type and item.get("normalized_value") == normalized:
            return item
    return None


def sources_hub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Список источников", callback_data="sources_list")],
            [InlineKeyboardButton("📚 Все источники", callback_data="sources_inventory")],
            [InlineKeyboardButton("➕ Добавить RSS", callback_data="source_add_rss")],
            [InlineKeyboardButton("➕ Добавить Telegram-канал", callback_data="source_add_telegram")],
            [InlineKeyboardButton("🧪 Проверить источники", callback_data="menu_sources_status")],
            [InlineKeyboardButton("🩺 Здоровье источников", callback_data="sources_health")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")],
        ]
    )


def source_card_keyboard(source_id: int, enabled: bool) -> InlineKeyboardMarkup:
    toggle = "⛔ Disable" if enabled else "✅ Enable"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(toggle, callback_data=f"source_toggle:{source_id}"),
                InlineKeyboardButton("🧪 Test", callback_data=f"source_test:{source_id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"source_delete:{source_id}"),
            ]
        ]
    )


def render_sources_status(reports, db, source_group_labels) -> str:
    total = len(reports)
    ok = sum(1 for report in reports if report.status == "ok")
    empty = sum(1 for report in reports if report.status == "empty")
    skipped = sum(1 for report in reports if report.status == "skipped")
    errors = sum(1 for report in reports if report.status == "error")
    lines = [
        "📡 Статус источников",
        "Чтобы посмотреть полный список источников: 📡 Источники → 📚 Все источники",
        "",
        f"Всего источников: {total}",
        f"Работают: {ok}",
        f"Пустые: {empty}",
        f"Отключены/пропущены: {skipped}",
        f"Ошибки: {errors}",
        "",
        "По группам:",
    ]
    for group, label in source_group_labels.items():
        group_reports = [report for report in reports if (report.source_group or "other") == group]
        if not group_reports:
            if group == "custom":
                lines.append(f"{label}: 0/0")
            continue
        group_ok = sum(1 for report in group_reports if report.status == "ok")
        group_skipped = sum(1 for report in group_reports if report.status == "skipped")
        suffix = f", пропущено {group_skipped}" if group_skipped else ""
        lines.append(f"{label}: {group_ok}/{len(group_reports)}{suffix}")

    skipped_reports = [report for report in reports if report.status == "skipped"]
    if skipped_reports:
        lines.extend(["", "Отключено/пропущено:"])
        for report in skipped_reports[:8]:
            lines.append(f"- {report.name}: {report.error or 'пропущено'}")

    problems = [report for report in reports if report.status in {"error", "empty"}]
    if problems:
        lines.extend(["", "Проблемы:"])
        for report in problems[:12]:
            if report.status == "error":
                lines.append(f"- {report.name}: {report.error or 'ошибка'}")
            else:
                lines.append(f"- {report.name}: 0 тем")
        if len(problems) > 12:
            lines.append("Показаны первые 12 проблем.")

    if db is not None:
        health_rows = db.list_source_health(limit=500)
        if not health_rows:
            lines.append("\nИстория здоровья пока пустая. Запусти /collect или проверку источников.")
        else:
            h_ok = sum(1 for row in health_rows if row["last_status"] == "ok")
            h_empty = sum(1 for row in health_rows if row["last_status"] == "empty")
            h_err = sum(1 for row in health_rows if row["last_status"] == "error")
            h_pause = sum(1 for row in health_rows if row["disabled_until"])
            lines.append(f"\nЗдоровье источников: ✅ {h_ok}, ⚠️ {h_empty}, ❌ {h_err}, ⏸ {h_pause}")
            lines.append("Подробно: 📡 Источники → 🩺 Здоровье источников")
    return "\n".join(lines)[:3900]


def _redact_source_text(text: str) -> str:
    result = text
    for name in (
        "BOT_TOKEN",
        "TELEGRAM_SESSION_STRING",
        "TELEGRAM_API_HASH",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "X_API_BEARER_TOKEN",
    ):
        value = os.getenv(name, "").strip()
        result = result.replace(name, "***")
        if value:
            result = result.replace(value, "***")
    return result


def render_sources_health(db: DraftDatabase) -> str:
    rows = db.list_source_health(limit=200)
    if not rows:
        return "История здоровья пока пустая. Запусти /collect или проверку источников."
    parts = ["🩺 Здоровье источников"]
    groups = [
        ("✅ Работают", lambda row: row["last_status"] == "ok"),
        ("⚠️ Пустые", lambda row: row["last_status"] == "empty"),
        ("❌ Ошибки", lambda row: row["last_status"] == "error"),
        ("⏸ На паузе", lambda row: bool(row["disabled_until"])),
    ]
    for label, predicate in groups:
        selected = [row for row in rows if predicate(row)]
        if not selected:
            continue
        parts.append(f"\n{label}:")
        for row in selected[:20]:
            line = f"[{row['source_type']}/{row['source_group']}] {row['source_name']}"
            if row["last_status"] == "error":
                line += (
                    f" - {str(row['last_error'] or 'ошибка')[:80]}, "
                    f"ошибок подряд: {int(row['consecutive_errors'] or 0)}"
                )
            if row["disabled_until"]:
                line += f" - пауза до {str(row['disabled_until'])[11:16]}"
            parts.append(line)
    return _redact_source_text("\n".join(parts))[:3900]


def _pack_inventory_blocks(blocks: list[tuple[str, list[dict]]], limit: int = 3500) -> list[str]:
    chunks: list[str] = []
    current_lines: list[str] = []
    icon_map = {"enabled": "✅", "skipped": "⏭️", "disabled": "⛔"}

    def append_line(line: str) -> None:
        nonlocal current_lines
        candidate = "\n".join([*current_lines, line])
        if current_lines and len(candidate) > limit:
            chunks.append("\n".join(current_lines).strip())
            current_lines = [line]
        else:
            current_lines.append(line)

    for title, rows in blocks:
        append_line(f"\n{title}:")
        if not rows:
            append_line("— пусто")
            continue
        for item in rows:
            icon = icon_map.get(item.get("status", "enabled"), "•")
            reason = str(item.get("reason") or "").strip()
            suffix = f" ({reason})" if reason else ""
            append_line(
                f"{icon} [{item.get('source_type')}/{item.get('group')}] "
                f"{item.get('name')} — {item.get('value')}{suffix}"
            )
    if current_lines:
        chunks.append("\n".join(current_lines).strip())
    return chunks


def render_sources_inventory(settings, db, detect_railway) -> list[str]:
    builtin = built_in_rss_sources(settings)
    env_rows = env_configured_sources(settings)
    managed = db_managed_sources(db)
    messages = [
        f"Всего источников: {len(builtin) + len(env_rows) + len(managed)}. "
        f"Встроенные: {len(builtin)}. Env: {len(env_rows)}. Мои: {len(managed)}."
    ]
    if detect_railway(settings.db_path):
        messages.append(
            "⚠️ DB_PATH сейчас local data/drafts.db. Источники, добавленные через бота, "
            "могут пропасть после redeploy. Лучше подключить Railway Volume и поставить "
            "DB_PATH=/data/drafts.db."
        )
    messages.extend(
        _pack_inventory_blocks(
            [
                ("Встроенные RSS", builtin),
                ("Env источники", env_rows),
                ("Мои источники (DB)", managed),
            ]
        )
    )
    return messages


async def run_sources_status_background(
    context,
    settings,
    db,
    run_collect,
    render_status,
    back_to_menu_keyboard,
    logger,
) -> None:
    started = time.perf_counter()
    try:
        _items, reports = await run_collect(settings=settings, db=db)
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=render_status(reports, db),
            reply_markup=back_to_menu_keyboard(),
        )
    except Exception:
        logger.exception("Background source diagnostics failed")
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text="Не удалось проверить источники. Попробуй ещё раз.",
        )
    finally:
        logger.info("Source diagnostics completed in %.2fs", time.perf_counter() - started)
        context.application.bot_data["sources_check_running"] = False


async def run_source_test_background(context, settings, db, row, run_collect, logger) -> None:
    source_id = int(row["id"])
    started = time.perf_counter()
    try:
        if row["source_type"] == "rss":
            feed_url, error = await asyncio.to_thread(discover_rss_feed_url, str(row["value"]))
            status = "ok" if feed_url else "error"
            db.update_managed_source_status(source_id, status, error)
            db.record_source_health(
                "rss",
                normalize_source_url(str(row["value"])),
                str(row["name"]),
                str(row["source_group"] or "custom"),
                status,
                error,
            )
            text = f"RSS test #{source_id}: OK: {feed_url}" if feed_url else f"RSS test #{source_id}: Ошибка: {error}"
            await context.bot.send_message(chat_id=settings.admin_id, text=text)
            return

        _items, reports = await run_collect(settings=settings, db=db)
        matched = [
            report
            for report in reports
            if str(report.url).endswith(str(row["value"])) or report.name == f"Telegram @{row['value']}"
        ]
        report = matched[0] if matched else None
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=f"Telegram test #{source_id}: {report.status if report else 'нет данных'}",
        )
    except Exception:
        logger.exception("Background source test failed for source_id=%s", source_id)
        await context.bot.send_message(
            chat_id=settings.admin_id,
            text=f"Не удалось проверить источник #{source_id}.",
        )
    finally:
        logger.info("Source test completed for source_id=%s in %.2fs", source_id, time.perf_counter() - started)
        running = context.application.bot_data.get("source_test_running")
        if isinstance(running, set):
            running.discard(source_id)


async def handle_sources_callback(
    update,
    context,
    data,
    edit_callback_message,
    sources_hub_keyboard,
    source_card_keyboard,
    run_source_test_background,
    render_sources_inventory,
) -> None:
    settings = context.bot_data["settings"]
    db = context.bot_data["db"]
    query = update.callback_query
    if not query:
        return

    if data == "sources_list":
        rows = db.list_managed_sources(include_disabled=True)
        if not rows:
            await edit_callback_message(
                query,
                "Пока нет пользовательских источников.",
                reply_markup=sources_hub_keyboard(),
            )
            return
        await edit_callback_message(query, f"Источников: {len(rows)}", reply_markup=sources_hub_keyboard())
        for row in rows:
            enabled = bool(row["enabled"])
            status = "✅" if enabled else "⛔"
            text = (
                f"#{row['id']} {status} [{row['source_type']}]\n"
                f"{row['name']}\n{row['value']}\nstatus: {row['last_status'] or '-'}"
            )
            if row["last_error"]:
                text += f"\nerr: {str(row['last_error'])[:120]}"
            await context.bot.send_message(
                chat_id=settings.admin_id,
                text=text,
                reply_markup=source_card_keyboard(int(row["id"]), enabled),
            )
        return

    if data == "sources_inventory":
        parts = render_sources_inventory(settings, db)
        await edit_callback_message(query, parts[0], reply_markup=sources_hub_keyboard())
        for part in parts[1:]:
            await context.bot.send_message(
                chat_id=settings.admin_id,
                text=part,
                reply_markup=sources_hub_keyboard(),
            )
        return

    if data == "source_add_rss":
        context.user_data["source_add_flow"] = {"type": "rss", "step": "name"}
        await edit_callback_message(
            query,
            "Введите название RSS-источника.",
            reply_markup=sources_hub_keyboard(),
        )
        return

    if data == "source_add_telegram":
        context.user_data["source_add_flow"] = {"type": "telegram", "step": "value"}
        await edit_callback_message(
            query,
            "Пришлите username канала или ссылку t.me/...",
            reply_markup=sources_hub_keyboard(),
        )
        return

    if data == "source_confirm_rss":
        flow = context.user_data.get("source_add_flow") or {}
        if flow.get("type") == "rss" and flow.get("step") == "confirm" and flow.get("feed_url"):
            try:
                db.create_managed_source(
                    "rss",
                    str(flow.get("name") or "RSS"),
                    str(flow["feed_url"]),
                    "custom",
                )
            except ValueError as exc:
                await edit_callback_message(
                    query,
                    f"Не удалось добавить источник: {exc}",
                    reply_markup=sources_hub_keyboard(),
                )
                return
            context.user_data.pop("source_add_flow", None)
            await edit_callback_message(query, "RSS-источник добавлен.", reply_markup=sources_hub_keyboard())
            return
        await edit_callback_message(query, "Нет данных для сохранения.", reply_markup=sources_hub_keyboard())
        return

    if data == "source_cancel_add":
        context.user_data.pop("source_add_flow", None)
        await edit_callback_message(query, "Добавление отменено.", reply_markup=sources_hub_keyboard())
        return

    if data.startswith("source_toggle:"):
        source_id = int(data.split(":", 1)[1])
        row = db.get_managed_source(source_id)
        if not row:
            await edit_callback_message(query, "Источник не найден.", reply_markup=sources_hub_keyboard())
            return
        db.update_managed_source_enabled(source_id, not bool(row["enabled"]))
        await edit_callback_message(query, "Обновил статус источника.", reply_markup=sources_hub_keyboard())
        return

    if data.startswith("source_delete:"):
        source_id = int(data.split(":", 1)[1])
        deleted = db.delete_managed_source(source_id)
        text = "Источник удалён." if deleted else "Источник не найден."
        await edit_callback_message(query, text, reply_markup=sources_hub_keyboard())
        return

    if data.startswith("source_test:"):
        source_id = int(data.split(":", 1)[1])
        row = db.get_managed_source(source_id)
        if not row:
            await edit_callback_message(query, "Источник не найден.", reply_markup=sources_hub_keyboard())
            return
        if context.application.bot_data.get("sources_check_running"):
            await edit_callback_message(
                query,
                "Проверка источников уже идёт. Дождись результата.",
                reply_markup=sources_hub_keyboard(),
            )
            return
        running = context.application.bot_data.setdefault("source_test_running", set())
        if source_id in running:
            await edit_callback_message(
                query,
                "Проверка этого источника уже идёт. Дождись результата.",
                reply_markup=sources_hub_keyboard(),
            )
            return
        running.add(source_id)
        progress = "Проверяю RSS..." if row["source_type"] == "rss" else "Проверяю источник..."
        await edit_callback_message(query, progress, reply_markup=sources_hub_keyboard())
        context.application.create_task(
            run_source_test_background(context=context, settings=settings, db=db, row=row)
        )
        return

    await edit_callback_message(query, "Неизвестное действие источников.", reply_markup=sources_hub_keyboard())


async def sources_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot import handlers

    settings = context.bot_data["settings"]
    db: DraftDatabase = context.bot_data["db"]
    user_id = update.effective_user.id if update.effective_user else None
    if not handlers._is_admin(user_id, settings.admin_id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    if update.message:
        await update.message.reply_text("Проверяю источники...")
    _items, reports = await handlers._run_collect_topics_with_diagnostics(settings=settings, db=db)
    if update.message:
        await update.message.reply_text(
            handlers._render_sources_status(reports, db),
            reply_markup=handlers._admin_reply_keyboard(),
        )
