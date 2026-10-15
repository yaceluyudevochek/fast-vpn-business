"""Тесты этапов 0–1: миграции схемы, аудитории рассылки, напоминание триальщикам."""
import asyncio
import os
import sqlite3
import sys
import tempfile

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from database import db  # noqa: E402
from services.reminder import SubscriptionReminder  # noqa: E402


def sql(path, query, params=()):
    conn = sqlite3.connect(path)
    cur = conn.execute(query, params)
    conn.commit()
    rows = cur.fetchall()
    conn.close()
    return rows


async def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    return path


async def test_migrations():
    # 1. Чистая БД получает все колонки и актуальную версию схемы
    path = await fresh_db()
    assert sql(path, "PRAGMA user_version")[0][0] == len(db._MIGRATIONS)
    assert "full_description" in {r[1] for r in sql(path, "PRAGMA table_info(tariff_types)")}
    assert "trial_nudge_status" in {r[1] for r in sql(path, "PRAGMA table_info(subscriptions)")}
    await db.init_db()  # повторный запуск ничего не ломает
    assert sql(path, "PRAGMA user_version")[0][0] == len(db._MIGRATIONS)

    # 2. "Боевая" БД v2.0.2: колонки активации уже есть, user_version = 0
    old = os.path.join(tempfile.mkdtemp(), "old.db")
    db.DB_PATH = path
    c = sqlite3.connect(path)
    c.execute("PRAGMA user_version = 0")
    c.execute("ALTER TABLE tariff_types DROP COLUMN full_description")
    c.execute("ALTER TABLE subscriptions DROP COLUMN trial_nudge_status")
    c.commit()
    c.close()
    await db.init_db()
    assert sql(path, "PRAGMA user_version")[0][0] == len(db._MIGRATIONS)
    print("OK  миграции: чистая БД, повторный запуск, БД с версией 0 и уже применёнными изменениями")


async def test_mailing_audiences():
    path = await fresh_db()
    future = "2099-01-01T00:00:00.000Z"
    past = "2020-01-01T00:00:00.000Z"

    def user(tid, role="user"):
        sql(path, "INSERT INTO users (telegram_id, role) VALUES (?, ?)", (tid, role))

    async def sub(tid, expiry, status="ACTIVE", n=[0]):
        n[0] += 1
        await db.create_subscription(tid, 1, 1, f"s{n[0]}", expiry, f"u{n[0]}", trial=False)
        sql(path, "UPDATE subscriptions SET status=? WHERE username_in_panel=?", (status, f"u{n[0]}"))

    for tid in (1, 2, 3, 5, 6, 7):
        user(tid)
    user(4, role="banned")

    await sub(1, future)                       # 1 — активная
    await sub(2, past, status="EXPIRED")       # 2 — истёкшая
    #                                          # 3 — подписок нет
    await sub(4, future)                       # 4 — забанен
    await sub(5, future)                       # 5 — активная + истёкшая => активная
    await sub(5, past, status="EXPIRED")
    await sub(6, future, status="DISABLED")    # 6 — отключена
    await sub(7, None)                         # 7 — дата не задана => не активна
    # отстающий статус: в БД EXPIRED, а дата в будущем (как было у 734092546)
    user(8)
    await sub(8, future, status="EXPIRED")

    assert sorted(await db.get_mailing_recipients("all")) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert await db.get_mailing_recipients("active") == [1, 5, 8]
    assert await db.get_mailing_recipients("expired") == [2, 7]
    assert await db.get_mailing_recipients("both") == [1, 2, 5, 7, 8]
    try:
        await db.get_mailing_recipients("bogus")
        raise AssertionError("ожидали ValueError")
    except ValueError:
        pass
    print("OK  рассылка: all / active / expired / both, бан и DISABLED исключены, статус не используется для активности")


class FakePanel:
    def __init__(self, used):
        self.used = used

    async def get_used_traffic_bytes(self, username):
        return self.used.get(username, 0) if isinstance(self.used, dict) else self.used


class FakeBot:
    def __init__(self, fail_with=None):
        self.sent = []
        self.fail_with = fail_with

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_with:
            raise Exception(self.fail_with)
        self.sent.append(chat_id)


async def test_trial_nudge():
    path = await fresh_db()
    future = "2099-01-01T00:00:00.000Z"
    past = "2020-01-01T00:00:00.000Z"

    async def trial(tid, hours_ago, expiry=future):
        sql(path, "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (tid,))
        sid = await db.create_subscription(tid, 1, 1, f"s{tid}", expiry, f"tg{tid}_1", trial=True)
        sql(path, "UPDATE subscriptions SET created_at = datetime('now', ?) WHERE id = ?", (f"-{hours_ago} hours", sid))
        return sid

    def status(sid):
        return sql(path, "SELECT trial_nudge_status FROM subscriptions WHERE id=?", (sid,))[0][0]

    s_idle = await trial(101, 3)          # не пользуется  -> напоминание
    s_busy = await trial(102, 3)          # 200 МБ         -> connected
    s_fresh = await trial(103, 1)         # слишком рано   -> ничего
    s_paid = await trial(104, 3)          # уже платил     -> ничего
    sql(path, "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status) VALUES ('p',104,1,150,'CONFIRMED')")
    s_old = await trial(105, 100)         # старше окна    -> ничего
    s_expired = await trial(106, 3, expiry=past)  # триал закончился -> connected
    s_unknown = await trial(107, 3)       # панель молчит  -> повторим позже

    used = {"tg101_1": 10 * 1024 * 1024, "tg102_1": 200 * 1024 * 1024, "tg107_1": None}
    bot = FakeBot()
    reminder = SubscriptionReminder(bot, panel_api=FakePanel(used))
    await reminder._check_trial_nudges()

    assert bot.sent == [101], bot.sent
    assert status(s_idle) == "sent"
    assert status(s_busy) == "connected"
    assert status(s_fresh) is None and status(s_paid) is None and status(s_old) is None
    assert status(s_expired) == "connected"
    assert status(s_unknown) is None

    # повторный проход (и "перезапуск бота" — новый экземпляр) ничего не дублирует
    reminder2 = SubscriptionReminder(bot, panel_api=FakePanel(used))
    await reminder2._check_trial_nudges()
    assert bot.sent == [101], bot.sent

    # когда панель отвечает — напоминание уходит
    used["tg107_1"] = 0
    await reminder2._check_trial_nudges()
    assert bot.sent == [101, 107] and status(s_unknown) == "sent"

    # пользователь заблокировал бота: считаем обработанным, не повторяем каждую минуту
    s_blocked = await trial(108, 3)
    blocked_bot = FakeBot(fail_with="Forbidden: bot was blocked by the user")
    await SubscriptionReminder(blocked_bot, panel_api=FakePanel(0))._check_trial_nudges()
    assert status(s_blocked) == "sent"

    # временный сбой отправки: статус не ставим, повторим на следующем проходе
    s_flaky = await trial(109, 3)
    flaky_bot = FakeBot(fail_with="Network error")
    await SubscriptionReminder(flaky_bot, panel_api=FakePanel(0))._check_trial_nudges()
    assert status(s_flaky) is None
    print("OK  триал-напоминание: отправка один раз, пользователи/оплатившие/свежие/старые пропускаются, перезапуск безопасен")


async def test_full_description():
    path = await fresh_db()
    assert (await db.get_tariff_type(1)).get("full_description") is None
    sql(path, "UPDATE tariff_types SET full_description = ? WHERE id = 1", ("<b>Три</b> устройства",))
    assert (await db.get_tariff_type(1))["full_description"] == "<b>Три</b> устройства"
    print("OK  полное описание тарифа сохраняется и читается")


async def main():
    await test_migrations()
    await test_mailing_audiences()
    await test_trial_nudge()
    await test_full_description()


if __name__ == "__main__":
    asyncio.run(main())
