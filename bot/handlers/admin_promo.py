"""Админка: промокоды (создание, список, досрочное завершение, продление, отчёт для рекламных кодов)."""
import logging
import re
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database import db as database
from database import promo_db
from services.promo import end_of_day_msk_to_utc_iso, format_valid_until, normalize_code

router = Router()
logger = logging.getLogger(__name__)

CODE_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")  # допустимо и в параметре deep-link ссылки
KIND_LABELS = {"regular": "Обычный", "ad": "Рекламный"}


class CreatePromo(StatesGroup):
    code = State()
    discount = State()
    limit = State()
    until = State()
    first_only = State()
    scope = State()
    partner = State()
    commission = State()
    confirm = State()


class EditPromo(StatesGroup):
    value = State()


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


async def _require_admin(callback: CallbackQuery) -> bool:
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора", show_alert=True)
        return False
    return True


async def _describe_scope(promo_id: int) -> str:
    plan_ids = await promo_db.get_scope_plan_ids(promo_id)
    if not plan_ids:
        return "все тарифы и планы"
    parts = []
    for plan_id in plan_ids:
        plan = await database.get_tariff_plan(plan_id)
        if plan:
            parts.append(f"{plan['tariff_name']} — {plan['days']} дн.")
    return "; ".join(parts) or "—"


def _limit_text(promo: dict, used: int) -> str:
    return f"{used} / {promo['max_activations']}" if promo["max_activations"] is not None else f"{used} / без лимита"


# ======================================================
# СПИСОК И КАРТОЧКА
# ======================================================

@router.callback_query(F.data == "admin_promos")
async def admin_promos(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin(callback):
        return
    await state.clear()

    promos = await promo_db.list_promos()
    rows = []
    for promo in promos[:30]:
        mark = "✅" if promo["is_active"] else "⛔"
        limit = promo["max_activations"] if promo["max_activations"] is not None else "∞"
        ad = " 📣" if promo["kind"] == "ad" else ""
        rows.append([_btn(f"{mark} {promo['code']}{ad} · −{promo['discount_percent']}% · {promo['used']}/{limit}",
                          f"promo_adm_view_{promo['id']}")])
    rows.append([_btn("➕ Создать промокод", "promo_adm_new")])
    rows.append([_btn("« Назад", "admin_back")])

    text = "🎟 <b>Промокоды</b>\n\n"
    text += "Выберите промокод или создайте новый." if promos else "Промокодов пока нет."
    await callback.message.edit_text(text, reply_markup=_kb(rows), parse_mode="HTML")
    await callback.answer()


async def show_promo_card(target, promo_id: int, as_new_message: bool = False):
    promo = await promo_db.get_promo(promo_id)
    if not promo:
        return False
    used = await promo_db.count_redemptions(promo_id)
    holds = await promo_db.count_pending_holds(promo_id)

    status = "✅ активен" if promo["is_active"] else "⛔ завершён"
    if promo["is_active"] and promo["valid_until"]:
        until = datetime.fromisoformat(promo["valid_until"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > until:
            status = "⌛ срок истёк"
    if promo["is_active"] and promo["max_activations"] is not None and used >= promo["max_activations"]:
        status = "🏁 лимит активаций исчерпан"

    lines = [
        f"🎟 <b>{promo['code']}</b> — {KIND_LABELS.get(promo['kind'], promo['kind'])}",
        f"Статус: {status}",
        f"Скидка: <b>{promo['discount_percent']}%</b>",
        f"Активации (оплаченные): <b>{_limit_text(promo, used)}</b>"
        + (f" (+{holds} в оплате)" if holds else ""),
        f"Действует до: <b>{format_valid_until(promo['valid_until'])}</b>",
        f"Только на первую оплату: {'да' if promo['first_payment_only'] else 'нет'}",
        f"Тарифы: {await _describe_scope(promo_id)}",
    ]
    if promo["kind"] == "ad":
        lines.append(f"Партнёр: {promo['partner_name'] or '—'}")
        lines.append(
            "Комиссия партнёра: "
            + (f"{promo['commission_percent']:g}%" if promo["commission_percent"] is not None else "не задана")
        )

    bot_info = await target.bot.get_me()
    lines.append(f"\nСсылка: <code>https://t.me/{bot_info.username}?start=promo_{promo['code']}</code>")

    rows = [
        [_btn("📊 Отчёт", f"promo_adm_report_{promo_id}")],
        [_btn("▶ Возобновить" if not promo["is_active"] else "⏹ Завершить досрочно", f"promo_adm_toggle_{promo_id}")],
        [_btn("📅 Изменить срок", f"promo_adm_until_{promo_id}"), _btn("➕ Лимит активаций", f"promo_adm_limit_{promo_id}")],
        [_btn("« К списку", "admin_promos")],
    ]
    text = "\n".join(lines)
    if as_new_message:
        await target.answer(text, reply_markup=_kb(rows), parse_mode="HTML")
    else:
        await target.edit_text(text, reply_markup=_kb(rows), parse_mode="HTML")
    return True


@router.callback_query(F.data.startswith("promo_adm_view_"))
async def promo_view(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    promo_id = int(callback.data.rsplit("_", 1)[1])
    if not await show_promo_card(callback.message, promo_id):
        await callback.answer("Промокод не найден", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("promo_adm_toggle_"))
async def promo_toggle(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    promo_id = int(callback.data.rsplit("_", 1)[1])
    promo = await promo_db.get_promo(promo_id)
    if not promo:
        await callback.answer("Промокод не найден", show_alert=True)
        return
    await promo_db.update_promo(promo_id, is_active=0 if promo["is_active"] else 1)
    await show_promo_card(callback.message, promo_id)
    await callback.answer("Промокод завершён" if promo["is_active"] else "Промокод возобновлён")


@router.callback_query(F.data.startswith("promo_adm_report_"))
async def promo_report_view(callback: CallbackQuery):
    if not await _require_admin(callback):
        return
    promo_id = int(callback.data.rsplit("_", 1)[1])
    promo = await promo_db.get_promo(promo_id)
    if not promo:
        await callback.answer("Промокод не найден", show_alert=True)
        return
    report = await promo_db.promo_report(promo_id)

    lines = [
        f"📊 <b>Отчёт по промокоду {promo['code']}</b>",
        f"Перешли по ссылке: <b>{report['link_users']}</b> чел.",
        f"Применили код к плану: <b>{report['applied_users']}</b> чел.",
        f"Оплат по коду: <b>{report['payments']}</b> (плательщиков: {report['paying_users']})",
        f"Выручка по коду: <b>{report['revenue']} ₽</b>",
        f"Выдано скидок: <b>{report['discount_given']} ₽</b>",
        f"Все оплаты привлечённых за всё время: <b>{report['lifetime_revenue']} ₽</b>",
    ]
    if promo["kind"] == "ad":
        lines.append(f"\nПартнёр: <b>{promo['partner_name'] or '—'}</b>")
        if promo["commission_percent"] is not None:
            pct = promo["commission_percent"]
            lines.append(f"Комиссия {pct:g}% от выручки по коду: <b>{report['revenue'] * pct / 100:.2f} ₽</b>")
            lines.append(f"Комиссия {pct:g}% от всех оплат привлечённых: <b>{report['lifetime_revenue'] * pct / 100:.2f} ₽</b>")
        else:
            lines.append("Комиссия не задана.")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=_kb([[_btn("« К промокоду", f"promo_adm_view_{promo_id}")]]),
        parse_mode="HTML"
    )
    await callback.answer()


# ======================================================
# ИЗМЕНЕНИЕ СРОКА И ЛИМИТА
# ======================================================

@router.callback_query(F.data.startswith("promo_adm_until_") | F.data.startswith("promo_adm_limit_"))
async def promo_edit_start(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin(callback):
        return
    _, _, action, promo_id = callback.data.split("_")
    await state.set_state(EditPromo.value)
    await state.update_data(promo_id=int(promo_id), action=action)

    prompt = (
        "Введите новую дату окончания в формате <b>ДД.ММ.ГГГГ</b> (включительно) или <b>-</b> для бессрочного."
        if action == "until" else
        "Введите новое максимальное число активаций (не меньше уже использованных) или <b>-</b> для безлимита."
    )
    await callback.message.answer(prompt, parse_mode="HTML")
    await callback.answer()


@router.message(EditPromo.value, F.text)
async def promo_edit_value(message: Message, state: FSMContext):
    if not await database.is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    promo_id, action = data["promo_id"], data["action"]
    text = message.text.strip()

    if action == "until":
        if text == "-":
            await promo_db.update_promo(promo_id, valid_until=None)
        else:
            try:
                day = datetime.strptime(text, "%d.%m.%Y")
            except ValueError:
                await message.answer("❌ Неверный формат. Пример: 31.12.2026 или - для бессрочного.")
                return
            await promo_db.update_promo(promo_id, valid_until=end_of_day_msk_to_utc_iso(day))
    else:
        if text == "-":
            await promo_db.update_promo(promo_id, max_activations=None)
        else:
            used = await promo_db.count_redemptions(promo_id)
            if not text.isdigit() or int(text) < max(1, used):
                await message.answer(f"❌ Введите число не меньше {max(1, used)} (уже использовано {used}) или -.")
                return
            await promo_db.update_promo(promo_id, max_activations=int(text))

    await state.clear()
    await message.answer("✅ Сохранено")
    await show_promo_card(message, promo_id, as_new_message=True)


# ======================================================
# СОЗДАНИЕ ПРОМОКОДА
# ======================================================

@router.callback_query(F.data == "promo_adm_new")
async def promo_new(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin(callback):
        return
    await state.clear()
    await callback.message.edit_text(
        "🎟 <b>Новый промокод</b>\n\nКакого типа?\n\n"
        "<b>Обычный</b> — скидка для всех (акции, праздники).\n"
        "<b>Рекламный</b> — то же, но с учётом партнёра: статистика переходов, оплат и расчёт его заработка.",
        reply_markup=_kb([
            [_btn("Обычный", "promo_adm_kind_regular"), _btn("Рекламный", "promo_adm_kind_ad")],
            [_btn("« Отмена", "admin_promos")],
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("promo_adm_kind_"))
async def promo_choose_kind(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin(callback):
        return
    kind = callback.data.rsplit("_", 1)[1]
    await state.set_state(CreatePromo.code)
    await state.update_data(kind=kind, plan_ids=[])
    await callback.message.edit_text(
        "Введите <b>сам промокод</b>: латинские буквы, цифры, _ и -, от 3 до 32 символов.\n"
        "Например: <code>WINTER2027</code>",
        reply_markup=_kb([[_btn("« Отмена", "admin_promos")]]),
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(CreatePromo.code, F.text)
async def promo_step_code(message: Message, state: FSMContext):
    code = normalize_code(message.text)
    if not CODE_RE.match(code):
        await message.answer("❌ Только латинские буквы, цифры, _ и -, длина 3–32. Попробуйте ещё раз.")
        return
    if await promo_db.get_promo_by_code(code):
        await message.answer("❌ Такой промокод уже существует. Введите другой.")
        return
    await state.update_data(code=code)
    await state.set_state(CreatePromo.discount)
    data = await state.get_data()
    low = 0 if data["kind"] == "ad" else 1
    await message.answer(
        f"Введите <b>процент скидки</b> от цены плана ({low}–99)."
        + ("\n<i>0 — код без скидки, только для учёта переходов и оплат.</i>" if low == 0 else ""),
        parse_mode="HTML"
    )


@router.message(CreatePromo.discount, F.text)
async def promo_step_discount(message: Message, state: FSMContext):
    data = await state.get_data()
    low = 0 if data["kind"] == "ad" else 1
    text = message.text.strip().rstrip("%")
    if not text.isdigit() or not (low <= int(text) <= 99):
        await message.answer(f"❌ Введите целое число от {low} до 99.")
        return
    await state.update_data(discount_percent=int(text))
    await state.set_state(CreatePromo.limit)
    await message.answer("Введите <b>максимальное число активаций</b> или <b>-</b> без лимита.", parse_mode="HTML")


@router.message(CreatePromo.limit, F.text)
async def promo_step_limit(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "-":
        limit = None
    elif text.isdigit() and int(text) >= 1:
        limit = int(text)
    else:
        await message.answer("❌ Введите положительное число или -.")
        return
    await state.update_data(max_activations=limit)
    await state.set_state(CreatePromo.until)
    await message.answer(
        "Введите <b>дату окончания</b> в формате <b>ДД.ММ.ГГГГ</b> (включительно) или <b>-</b> бессрочно.",
        parse_mode="HTML"
    )


@router.message(CreatePromo.until, F.text)
async def promo_step_until(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "-":
        valid_until = None
    else:
        try:
            day = datetime.strptime(text, "%d.%m.%Y")
        except ValueError:
            await message.answer("❌ Неверный формат. Пример: 31.12.2026 или -.")
            return
        valid_until = end_of_day_msk_to_utc_iso(day)
        if datetime.fromisoformat(valid_until.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            await message.answer("❌ Эта дата уже прошла.")
            return
    await state.update_data(valid_until=valid_until)
    await state.set_state(CreatePromo.first_only)
    await message.answer(
        "Промокод действует только на <b>первую оплату</b> пользователя?",
        reply_markup=_kb([[_btn("Да, только первая", "promo_adm_first_1"), _btn("Нет, на любую", "promo_adm_first_0")]]),
        parse_mode="HTML"
    )


@router.callback_query(CreatePromo.first_only, F.data.startswith("promo_adm_first_"))
async def promo_step_first_only(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin(callback):
        return
    await state.update_data(first_payment_only=callback.data.endswith("_1"))
    await state.set_state(CreatePromo.scope)
    await _render_scope(callback.message, state, edit=True)
    await callback.answer()


async def _render_scope(message: Message, state: FSMContext, edit: bool):
    """Выбор тарифов и планов, на которые действует промокод"""
    data = await state.get_data()
    selected = set(data.get("plan_ids", []))

    rows = []
    for tariff_type in await database.get_all_tariff_types():
        plans = await database.get_tariff_plans(tariff_type["id"])
        if not plans:
            continue
        ids = {plan["id"] for plan in plans}
        all_on = ids <= selected
        rows.append([_btn(f"{'✅' if all_on else '⬜'} {tariff_type['name']} — все планы", f"promo_adm_tt_{tariff_type['id']}")])
        for plan in plans:
            mark = "✅" if plan["id"] in selected else "⬜"
            rows.append([_btn(f"   {mark} {plan['days']} дн. — {plan['price']} ₽", f"promo_adm_tp_{plan['id']}")])
    rows.append([_btn("✔ Готово", "promo_adm_scope_done")])
    rows.append([_btn("« Отмена", "admin_promos")])

    text = (
        "На какие тарифы и планы действует промокод?\n\n"
        "Отметьте нужные. Если не отметить ничего, промокод будет действовать на <b>все планы</b>, "
        "включая те, что появятся позже."
    )
    if edit:
        await message.edit_text(text, reply_markup=_kb(rows), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=_kb(rows), parse_mode="HTML")


@router.callback_query(CreatePromo.scope, F.data.startswith("promo_adm_tt_") | F.data.startswith("promo_adm_tp_"))
async def promo_scope_toggle(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin(callback):
        return
    _, _, kind, entity_id = callback.data.split("_")
    entity_id = int(entity_id)
    data = await state.get_data()
    selected = set(data.get("plan_ids", []))

    if kind == "tt":
        ids = {plan["id"] for plan in await database.get_tariff_plans(entity_id)}
        selected = selected - ids if ids <= selected else selected | ids
    else:
        selected ^= {entity_id}

    await state.update_data(plan_ids=sorted(selected))
    await _render_scope(callback.message, state, edit=True)
    await callback.answer()


@router.callback_query(CreatePromo.scope, F.data == "promo_adm_scope_done")
async def promo_scope_done(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin(callback):
        return
    data = await state.get_data()
    if data["kind"] == "ad":
        await state.set_state(CreatePromo.partner)
        await callback.message.answer(
            "Введите <b>имя партнёра</b> (кто распространяет код) или <b>-</b>, чтобы пропустить.",
            parse_mode="HTML"
        )
    else:
        await _show_confirm(callback.message, state)
    await callback.answer()


@router.message(CreatePromo.partner, F.text)
async def promo_step_partner(message: Message, state: FSMContext):
    text = message.text.strip()
    await state.update_data(partner_name=None if text == "-" else text[:100])
    await state.set_state(CreatePromo.commission)
    await message.answer(
        "Введите <b>комиссию партнёра</b> в процентах от выручки (0–100) или <b>-</b>, если пока не задана. "
        "Отчёт посчитает заработок по этой ставке.",
        parse_mode="HTML"
    )


@router.message(CreatePromo.commission, F.text)
async def promo_step_commission(message: Message, state: FSMContext):
    text = message.text.strip().rstrip("%").replace(",", ".")
    if text == "-":
        commission = None
    else:
        try:
            commission = float(text)
        except ValueError:
            commission = -1
        if not (0 <= commission <= 100):
            await message.answer("❌ Введите число от 0 до 100 или -.")
            return
    await state.update_data(commission_percent=commission)
    await _show_confirm(message, state)


async def _show_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(CreatePromo.confirm)

    scope = "все тарифы и планы"
    if data.get("plan_ids"):
        names = []
        for plan_id in data["plan_ids"]:
            plan = await database.get_tariff_plan(plan_id)
            if plan:
                names.append(f"{plan['tariff_name']} {plan['days']} дн.")
        scope = "; ".join(names)

    lines = [
        "🎟 <b>Проверьте промокод</b>\n",
        f"Код: <b>{data['code']}</b> ({KIND_LABELS[data['kind']]})",
        f"Скидка: <b>{data['discount_percent']}%</b>",
        f"Лимит активаций: <b>{data['max_activations'] if data['max_activations'] is not None else 'без лимита'}</b>",
        f"Действует до: <b>{format_valid_until(data['valid_until'])}</b>",
        f"Только на первую оплату: {'да' if data['first_payment_only'] else 'нет'}",
        f"Тарифы: {scope}",
    ]
    if data["kind"] == "ad":
        commission = data.get("commission_percent")
        lines.append(f"Партнёр: {data.get('partner_name') or '—'}")
        lines.append(f"Комиссия: {f'{commission:g}%' if commission is not None else 'не задана'}")

    await message.answer(
        "\n".join(lines),
        reply_markup=_kb([[_btn("✅ Создать", "promo_adm_create"), _btn("❌ Отмена", "admin_promos")]]),
        parse_mode="HTML"
    )


@router.callback_query(CreatePromo.confirm, F.data == "promo_adm_create")
async def promo_create(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin(callback):
        return
    data = await state.get_data()
    if await promo_db.get_promo_by_code(data["code"]):
        await callback.answer("Такой промокод уже существует", show_alert=True)
        return

    promo_id = await promo_db.create_promo(
        {**data, "created_by": callback.from_user.id},
        data.get("plan_ids", [])
    )
    await state.clear()
    await callback.answer("Промокод создан")
    await show_promo_card(callback.message, promo_id, as_new_message=True)
