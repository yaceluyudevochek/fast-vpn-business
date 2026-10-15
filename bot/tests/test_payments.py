"""Регрессионные тесты платёжного пути: одна транзакция — ровно одно начисление дней."""
import asyncio
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from database import db  # noqa: E402
from handlers import payment as pay  # noqa: E402
from services import payment_webhook as pw  # noqa: E402


class FakePanel:
    def __init__(self):
        self.extend_calls = 0
        self.fail_next = False

    async def extend_subscription_by_username(self, *a, **k):
        self.extend_calls += 1
        if self.fail_next:
            self.fail_next = False
            return None
        return "2027-01-11T11:12:59.000Z"

    async def update_user_status_by_username(self, *a, **k):
        return True


class FakeCallback:
    def __init__(self, tid):
        self.data = f"check_payment_{tid}"

    async def answer(self, *a, **k):
        return None


async def noop(*a, **k):
    return None


def add_tx(path, tid, status, activated=False):
    c = sqlite3.connect(path)
    c.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (991033531)")
    c.execute(
        "INSERT INTO payment_transactions (transaction_id, telegram_id, plan_id, amount, status, payload, activated_at) "
        "VALUES (?, 991033531, 2, 400, ?, '991033531_2_1791867776', ?)",
        (tid, status, "2026-10-13 05:04:11" if activated else None))
    c.commit()
    c.close()


async def setup():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.DB_PATH = path
    await db.init_db()
    c = sqlite3.connect(path)
    c.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (991033531)")
    c.commit()
    c.close()
    await db.create_subscription(991033531, 1, 2, "s", "2026-10-13T11:12:59.241Z", "tg991033531_1", trial=False)
    panel = FakePanel()
    pw.panel_api = panel
    pw.notify_user_success = noop
    pw.notify_admin_error = noop
    pay.show_screen = noop
    return path, panel


async def main():
    path, panel = await setup()

    # кнопка «Проверить оплату» -> потом webhook (инцидент v2.0.2)
    add_tx(path, "t1", "PENDING")
    tx = await db.get_transaction("t1")
    await db.update_transaction_status("t1", "CONFIRMED")
    assert await pw.activate_transaction(tx) is True
    await db.update_transaction_status("t1", "CONFIRMED")
    assert await pw.activate_transaction(await db.get_transaction("t1")) is False
    assert panel.extend_calls == 1
    print("OK  кнопка + webhook = одно продление")

    # дубль webhook
    add_tx(path, "t2", "PENDING")
    await db.update_transaction_status("t2", "CONFIRMED")
    assert await pw.activate_transaction(await db.get_transaction("t2")) is True
    assert await pw.activate_transaction(await db.get_transaction("t2")) is False
    assert panel.extend_calls == 2
    print("OK  webhook + дубль = одно продление")

    # сбой панели -> повтор срабатывает
    add_tx(path, "t3", "PENDING")
    await db.update_transaction_status("t3", "CONFIRMED")
    panel.fail_next = True
    assert await pw.activate_transaction(await db.get_transaction("t3")) is False
    assert await pw.activate_transaction(await db.get_transaction("t3")) is True
    assert panel.extend_calls == 4
    print("OK  после сбоя следующая попытка активирует")

    # параллельные вызовы
    add_tx(path, "t4", "PENDING")
    await db.update_transaction_status("t4", "CONFIRMED")
    before = panel.extend_calls
    txs = [await db.get_transaction("t4") for _ in range(5)]
    results = await asyncio.gather(*[pw.activate_transaction(t) for t in txs])
    assert sum(results) == 1 and panel.extend_calls == before + 1
    print("OK  5 параллельных вызовов = одно продление")

    # обработчик кнопки: повторные нажатия безопасны
    add_tx(path, "b1", "CONFIRMED", activated=True)
    before = panel.extend_calls
    for _ in range(3):
        await pay.check_payment(FakeCallback("b1"))
    assert panel.extend_calls == before

    add_tx(path, "b2", "CONFIRMED", activated=False)
    for _ in range(3):
        await pay.check_payment(FakeCallback("b2"))
    assert panel.extend_calls == before + 1

    add_tx(path, "b3", "PENDING")

    async def platega_confirmed(tid):
        return {"status": "CONFIRMED"}
    pay.platega_api = SimpleNamespace(check_payment_status=platega_confirmed)
    for _ in range(3):
        await pay.check_payment(FakeCallback("b3"))
    assert panel.extend_calls == before + 2
    print("OK  кнопка «Проверить оплату»: 3 нажатия подряд — одно продление, без ошибок")


if __name__ == "__main__":
    asyncio.run(main())
