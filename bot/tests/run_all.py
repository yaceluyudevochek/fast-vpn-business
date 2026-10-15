"""Запуск всех тестов:  python tests/run_all.py  (на временных БД, боевые данные не затрагиваются)"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_payments  # noqa: E402
import test_promo  # noqa: E402
import test_stage1  # noqa: E402
import test_wiring  # noqa: E402


async def main():
    for module in (test_payments, test_stage1, test_promo, test_wiring):
        print(f"== {module.__name__}")
        await module.main()
    print("\nВсе тесты пройдены")


if __name__ == "__main__":
    asyncio.run(main())
