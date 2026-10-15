"""Тесты промокодов: проверка, цена со скидкой, платёжный путь, учёт по факту оплаты, лимиты, отчёт."""
import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config as cfg
from database import db, promo_db
from handlers import handlers as hh
from handlers import payment as pay
from handlers import promo as promo_handlers
from services import payment_webhook as pw
from services import promo as promo_service
from services.promo import apply_discount, check_promo


def sql(path, query, params=()):
    conn = sqlite3.connect(path)
    cur = conn.execute(query, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


def iso(delta_days):
    return (datetime.now(timezone.utc) + timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    return path


def add_user(path, tid):
    sql(path, "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (tid,))


def make_state(user_id):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


class Recorder:
    def __init__(self):
        self.screens = []
        self.admin_alerts = []

    async def show_screen(self, target, key, text, kb=None):
        self.screens.append(text)

    async def notify_admin_error(self, user_id, message):
        self.admin_alerts.append(message)


async def noop(*a, **k):
    return None


class FakePanel:
    def __init__(self):
        self.extend_calls = 0

    async def extend_subscription_by_username(self, *a, **k):
        self.extend_calls += 1
        return "2027-01-11T11:12:59.000Z"

    async def update_user_status_by_username(self, *a, **k):
        return True


class FakeCallback:
    def __init__(self, user_id, data):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


class FakeMessage:
    def __init__(self, user_id, text):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.replies = []

    async def delete(self):
        return None

    async def answer(self, text, **kwargs):
        self.replies.append(text)


async def test_discount_math():
    cases = {(150, 15): 128, (400, 50): 200, (150, 50): 75, (99, 99): 1, (800, 10): 720, (250, 0): 250, (1, 50): 1}
    for (price, pct), expected in cases.items():
        assert apply_discount(price, pct) == expected, (price, pct, apply_discount(price, pct), expected)
    print("OK  скидка: округление до рубля, минимум 1 ₽, 0% не меняет цену")


async def test_check_promo():
    path = await fresh_db()
    for tid in (1, 2, 3):
        add_user(path, tid)
    plan30 = await db.get_tariff_plan(1)
    plan90 = await db.get_tariff_plan(2)

    async def new(code, **kw):
        data = {"code": code, "discount_percent": 20, **kw}
        return await promo_db.create_promo(data, kw.pop("plans", []))

    await promo_db.create_promo({"code": "WINTER", "discount_percent": 15, "valid_until": iso(10)}, [])
    r = await check_promo("winter", 1, plan30)  # регистр не важен
    assert r.ok and r.final_price == 128 and r.discount == 22, r

    assert (await check_promo("NOPE", 1, plan30)).message == promo_service.MSG_NOT_FOUND

    promo_id = (await promo_db.get_promo_by_code("WINTER"))["id"]
    await promo_db.update_promo(promo_id, is_active=0)
    assert (await check_promo("WINTER", 1, plan30)).message == promo_service.MSG_NOT_FOUND
    await promo_db.update_promo(promo_id, is_active=1, valid_until=iso(-1))
    assert (await check_promo("WINTER", 1, plan30)).message == promo_service.MSG_EXPIRED

    # область действия: только план 30 дней
    await promo_db.create_promo({"code": "ONLY30", "discount_percent": 50}, [plan30["id"]])
    assert (await check_promo("ONLY30", 1, plan30)).ok
    assert (await check_promo("ONLY30", 1, plan90)).message == promo_service.MSG_WRONG_PLAN

    # лимит активаций: оплаченные + открытые счета других пользователей занимают слоты
    limited_id = await promo_db.create_promo({"code": "LIM2", "discount_percent": 10, "max_activations": 2}, [])
    for tid in (1, 2):
        sql(path, "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status, promo_id, original_amount) "
                  "VALUES (?, ?, 1, 135, 'CONFIRMED', ?, 150)", (f"l{tid}", tid, limited_id))
        await promo_db.record_redemption(await db.get_transaction(f"l{tid}"))
    assert (await check_promo("LIM2", 3, plan30)).message == promo_service.MSG_EXHAUSTED
    assert (await check_promo("LIM2", 1, plan30)).message == promo_service.MSG_USED  # использован этим же пользователем

    # открытый счёт другого пользователя занимает слот
    held_id = await promo_db.create_promo({"code": "HOLD1", "discount_percent": 10, "max_activations": 1}, [])
    sql(path, "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status, promo_id, original_amount) "
              "VALUES ('h1', 2, 1, 135, 'PENDING', ?, 150)", (held_id,))
    assert (await check_promo("HOLD1", 3, plan30)).message == promo_service.MSG_EXHAUSTED
    assert (await check_promo("HOLD1", 2, plan30)).ok  # свой же открытый счёт слот не блокирует

    # только на первую оплату
    await promo_db.create_promo({"code": "FIRST", "discount_percent": 10, "first_payment_only": True}, [])
    assert (await check_promo("FIRST", 3, plan30)).ok
    sql(path, "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status) VALUES ('old', 3, 1, 150, 'CONFIRMED')")
    assert (await check_promo("FIRST", 3, plan30)).message == promo_service.MSG_FIRST_ONLY
    print("OK  проверка кода: регистр, неактивен, срок, тарифы/планы, лимит с открытыми счетами, повтор, первая оплата")


async def test_payment_flow_and_redemption():
    path = await fresh_db()
    add_user(path, 10)
    add_user(path, 11)
    await db.create_subscription(10, 1, 2, "s", "2026-10-13T11:12:59.241Z", "tg10_1", trial=False)

    rec = Recorder()
    pay.show_screen = rec.show_screen
    hh.show_screen = rec.show_screen
    pw.panel_api = FakePanel()
    pw.notify_user_success = noop
    pw.notify_admin_error = rec.notify_admin_error

    created = []

    async def create_payment(**kwargs):
        created.append(kwargs)
        return {"transactionId": f"tx{len(created)}", "redirect": "https://pay"}
    pay.platega_api = SimpleNamespace(create_payment=create_payment)

    promo_id = await promo_db.create_promo(
        {"code": "SALE", "discount_percent": 25, "max_activations": 1}, [2])  # план 90 дней
    plan = await db.get_tariff_plan(2)
    assert plan["price"] == 250  # в чистой БД план на 90 дней стоит 250 ₽

    # экран плана показывает цену со скидкой после применения кода
    state = make_state(10)
    await state.update_data(promo_code="SALE")
    await hh.render_select_plan(SimpleNamespace(), 10, 2, state)
    assert "188 руб." in rec.screens[-1] and "SALE" in rec.screens[-1], rec.screens[-1]

    # создание счёта: в Platega уходит цена со скидкой, в БД — промокод и исходная цена
    await state.update_data(plan_id=2)
    await state.set_state(pay.PaymentMethod.selecting_method)
    await pay.process_payment_method(FakeCallback(10, "payment_method_2"), state)
    assert created[-1]["amount"] == 188
    row = sql(path, "SELECT amount, promo_id, original_amount, status FROM payment_transactions WHERE transaction_id='tx1'")[0]
    assert row == (188, promo_id, 250, "PENDING"), row
    assert await promo_db.count_redemptions(promo_id) == 0  # пока не оплачено — не применён

    # оплата: webhook с корректной суммой — активация + применение, без алертов
    request = SimpleNamespace(
        headers={"X-MerchantId": cfg.PLATEGA_MERCHANT_ID, "X-Secret": cfg.PLATEGA_SECRET_KEY},
        json=lambda: _async({"id": "tx1", "amount": 188.0, "currency": "RUB", "status": "CONFIRMED",
                              "paymentMethod": 2, "payload": "10_2_x"}))
    response = await pw.handle_platega_callback(request)
    assert response.status == 200
    assert pw.panel_api.extend_calls == 1
    assert await promo_db.count_redemptions(promo_id) == 1
    assert rec.admin_alerts == []

    # повторный callback: ни второго продления, ни второго применения
    await pw.handle_platega_callback(request)
    assert pw.panel_api.extend_calls == 1 and await promo_db.count_redemptions(promo_id) == 1

    # лимит исчерпан: второй пользователь код уже не применит
    assert (await check_promo("SALE", 11, plan)).message == promo_service.MSG_EXHAUSTED

    # расхождение суммы: админ получает предупреждение, подписка при этом активируется
    sql(path, "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status, payload) VALUES ('tx9', 10, 2, 250, 'PENDING', '10_2_1790000000')")
    bad = SimpleNamespace(
        headers=request.headers,
        json=lambda: _async({"id": "tx9", "amount": 150.0, "currency": "RUB", "status": "CONFIRMED",
                              "paymentMethod": 2, "payload": "10_2_y"}))
    await pw.handle_platega_callback(bad)
    assert len(rec.admin_alerts) == 1 and pw.panel_api.extend_calls == 2
    print("OK  платёжный путь: цена со скидкой в счёте и экранах, применение только после оплаты и один раз, "
          "дубль callback безопасен, расхождение суммы — алерт админу")


async def _async(value):
    return value


async def test_user_input_and_rate_limit():
    path = await fresh_db()
    add_user(path, 20)
    rec = Recorder()
    hh.show_screen = rec.show_screen
    promo_handlers.show_screen = rec.show_screen
    await promo_db.create_promo({"code": "HELLO", "discount_percent": 10}, [])

    state = make_state(20)
    await state.set_state(promo_handlers.PromoInput.code)
    await state.update_data(promo_plan_id=1, promo_sub_id=0)

    # неверный код — сообщение об ошибке, код не сохраняется
    msg = FakeMessage(20, "nope")
    await promo_handlers.promo_code_input(msg, state)
    assert "не найден" in msg.replies[-1] and (await state.get_data()).get("promo_code") is None
    assert await state.get_state() == promo_handlers.PromoInput.code.state

    # верный код (в любом регистре) сохраняется, режим ввода закрывается, экран показывает скидку
    msg = FakeMessage(20, "  hello ")
    await promo_handlers.promo_code_input(msg, state)
    assert (await state.get_data())["promo_code"] == "HELLO" and await state.get_state() is None
    assert "HELLO" in rec.screens[-1] and "135" in rec.screens[-1], rec.screens[-1]
    report = await promo_db.promo_report((await promo_db.get_promo_by_code("HELLO"))["id"])
    assert report["applied_users"] == 1

    # подбор кодов: после 5 неверных попыток ввод блокируется
    promo_handlers._failed_attempts.clear()
    state2 = make_state(21)
    add_user(path, 21)
    await state2.set_state(promo_handlers.PromoInput.code)
    await state2.update_data(promo_plan_id=1, promo_sub_id=0)
    for i in range(5):
        await promo_handlers.promo_code_input(FakeMessage(21, f"bad{i}"), state2)
    blocked = FakeMessage(21, "HELLO")
    await promo_handlers.promo_code_input(blocked, state2)
    assert "Слишком много" in blocked.replies[-1] and (await state2.get_data()).get("promo_code") is None
    print("OK  ввод кода: неверный/верный (регистр), экран со скидкой, событие 'applied', защита от подбора")


async def test_ad_report():
    path = await fresh_db()
    for tid in (30, 31, 32):
        add_user(path, tid)
    ad_id = await promo_db.create_promo(
        {"code": "TIKTOK67", "kind": "ad", "discount_percent": 10, "partner_name": "Иван", "commission_percent": 20}, [])

    for tid in (30, 31, 32):
        await promo_db.record_event(ad_id, tid, "link")
    for tid in (30, 31):
        await promo_db.record_event(ad_id, tid, "applied")

    def paid(tid, txid, amount, original=None, promo=None, when="2026-10-01 10:00:00"):
        sql(path, "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status, promo_id, original_amount, created_at) "
                  "VALUES (?, ?, 1, ?, 'CONFIRMED', ?, ?, ?)", (txid, tid, amount, promo, original, when))

    paid(30, "a1", 135, 150, ad_id)                       # оплата по коду
    paid(31, "a2", 135, 150, ad_id)                       # оплата по коду
    paid(30, "a3", 150, when="2026-10-20 10:00:00")       # позже продлил уже без кода
    paid(32, "a4", 150)                                   # не использовал код — не его пользователь
    paid(30, "a0", 150, when="2026-09-01 10:00:00")       # до кода — в доход партнёра не входит
    for txid in ("a1", "a2"):
        await promo_db.record_redemption(await db.get_transaction(txid))

    report = await promo_db.promo_report(ad_id)
    assert report["link_users"] == 3 and report["applied_users"] == 2
    assert report["payments"] == 2 and report["paying_users"] == 2
    assert report["revenue"] == 270 and report["discount_given"] == 30
    assert report["lifetime_revenue"] == 135 + 135 + 150, report
    print("OK  отчёт рекламного кода: переходы/применения/оплаты, выручка, скидки, доход привлечённых за всё время")



class AdminMessage:
    """Сообщение админа + бот-заглушка: запоминает всё, что бот отправил или отредактировал"""
    def __init__(self, user_id=1, text=None):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.sent = []

        async def get_me():
            return SimpleNamespace(username="testbot")
        self.bot = SimpleNamespace(get_me=get_me)

    async def answer(self, text, **kwargs):
        self.sent.append(text)

    async def edit_text(self, text, **kwargs):
        self.sent.append(text)


class AdminCallback:
    def __init__(self, data, message, user_id=1):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=user_id)

    async def answer(self, *a, **k):
        return None


async def test_admin_flow():
    from handlers import admin_promo as ap

    path = await fresh_db()
    sql(path, "INSERT INTO users (telegram_id, role) VALUES (1, 'admin')")
    state = make_state(1)
    ui = AdminMessage()

    async def step(text):
        msg = AdminMessage(text=text)
        msg.bot, msg.sent = ui.bot, ui.sent
        return msg

    # рекламный промокод: тип -> код -> скидка -> лимит -> срок -> только первая -> планы -> партнёр -> комиссия
    await ap.promo_choose_kind(AdminCallback("promo_adm_kind_ad", ui), state)
    await ap.promo_step_code(await step("bad code!"), state)           # неверный формат
    assert await state.get_state() == ap.CreatePromo.code.state
    await ap.promo_step_code(await step("tiktok67"), state)
    await ap.promo_step_discount(await step("0"), state)               # для рекламного 0% допустим
    await ap.promo_step_limit(await step("100"), state)
    await ap.promo_step_until(await step("01.01.2020"), state)         # прошедшая дата отклоняется
    assert await state.get_state() == ap.CreatePromo.until.state
    await ap.promo_step_until(await step("31.12.2099"), state)
    await ap.promo_step_first_only(AdminCallback("promo_adm_first_1", ui), state)
    await ap.promo_scope_toggle(AdminCallback("promo_adm_tp_1", ui), state)       # план 30 дн.
    await ap.promo_scope_toggle(AdminCallback("promo_adm_tt_2", ui), state)       # все планы типа 2 (Premium)
    selected = set((await state.get_data())["plan_ids"])
    assert selected == {1, 4, 5, 6}, selected
    await ap.promo_scope_toggle(AdminCallback("promo_adm_tt_2", ui), state)       # повторный клик снимает
    assert set((await state.get_data())["plan_ids"]) == {1}
    await ap.promo_scope_done(AdminCallback("promo_adm_scope_done", ui), state)
    await ap.promo_step_partner(await step("Иван"), state)
    await ap.promo_step_commission(await step("120"), state)           # >100 отклоняется
    assert await state.get_state() == ap.CreatePromo.commission.state
    await ap.promo_step_commission(await step("20"), state)
    await ap.promo_create(AdminCallback("promo_adm_create", ui), state)

    promo = await promo_db.get_promo_by_code("TIKTOK67")
    assert promo["kind"] == "ad" and promo["discount_percent"] == 0 and promo["max_activations"] == 100
    assert promo["first_payment_only"] == 1 and promo["partner_name"] == "Иван" and promo["commission_percent"] == 20
    assert await promo_db.get_scope_plan_ids(promo["id"]) == [1]
    assert "https://t.me/testbot?start=promo_TIKTOK67" in ui.sent[-1]

    # дубликат кода отклоняется
    await ap.promo_choose_kind(AdminCallback("promo_adm_kind_regular", ui), state)
    await ap.promo_step_code(await step("TIKTOK67"), state)
    assert await state.get_state() == ap.CreatePromo.code.state
    await state.clear()

    # досрочное завершение и возобновление, продление срока и лимита
    await ap.promo_toggle(AdminCallback(f"promo_adm_toggle_{promo['id']}", ui), )
    assert (await promo_db.get_promo(promo["id"]))["is_active"] == 0
    await ap.promo_toggle(AdminCallback(f"promo_adm_toggle_{promo['id']}", ui))
    assert (await promo_db.get_promo(promo["id"]))["is_active"] == 1

    await ap.promo_edit_start(AdminCallback(f"promo_adm_limit_{promo['id']}", ui), state)
    await ap.promo_edit_value(await step("250"), state)
    assert (await promo_db.get_promo(promo["id"]))["max_activations"] == 250
    await ap.promo_edit_start(AdminCallback(f"promo_adm_until_{promo['id']}", ui), state)
    await ap.promo_edit_value(await step("-"), state)
    assert (await promo_db.get_promo(promo["id"]))["valid_until"] is None

    # список и отчёт открываются без ошибок
    await ap.admin_promos(AdminCallback("admin_promos", ui), state)
    await ap.promo_report_view(AdminCallback(f"promo_adm_report_{promo['id']}", ui))
    assert "Отчёт по промокоду TIKTOK67" in ui.sent[-1]
    print("OK  админка: создание рекламного кода по шагам с валидацией, выбор планов, завершение/возобновление, срок, лимит, список, отчёт")

async def main():
    await test_discount_math()
    await test_check_promo()
    await test_payment_flow_and_redemption()
    await test_user_input_and_rate_limit()
    await test_ad_report()
    await test_admin_flow()


if __name__ == "__main__":
    asyncio.run(main())
