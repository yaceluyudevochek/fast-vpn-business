import asyncio
import aiohttp
import logging
import json
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class PlategaAPI:
    def __init__(self, merchant_id: str, secret_key: str, base_url: str = "https://app.platega.io"):
        self.merchant_id = merchant_id
        self.secret_key = secret_key
        self.base_url = base_url.rstrip('/')
        self._session = None
        self.timeout = aiohttp.ClientTimeout(total=60, connect=30, sock_read=30)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def _prepare_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-MerchantId": self.merchant_id,
            "X-Secret": self.secret_key
        }

    async def _request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        session = await self._get_session()
        headers = await self._prepare_headers()
        url = f"{self.base_url}{endpoint}"

        logger.info(f"🌐 Platega request: {method} {url}")
        logger.info(f"📤 Headers: { {k: v for k, v in headers.items() if k != 'X-Secret'} }")

        try:
            # Стандартная проверка SSL (убрали принудительное отключение)
            async with session.request(method, url, headers=headers, **kwargs) as response:
                response_text = await response.text()
                logger.info(f"📥 Platega response status: {response.status}")
                logger.info(f"📥 Response body: {response_text[:500]}")

                if response.status in [200, 201]:
                    return json.loads(response_text) if response_text else {}

                logger.error(f"Platega API error {response.status}: {response_text}")
                return {"error": True, "status": response.status, "details": response_text}

        except aiohttp.ClientConnectorError as e:
            logger.error(f"❌ Connection error: {e}")
            logger.error("Проверьте: 1) Доступность app.platega.io 2) DNS настройки 3) Интернет")
            return None
        except asyncio.TimeoutError:
            logger.error("❌ Timeout error - сервер не отвечает")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON decode error: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Request error: {e}", exc_info=True)
            return None

    # ======================================================
    # СОЗДАНИЕ ПЛАТЕЖА
    # ======================================================

    async def create_payment(
            self,
            amount: int,
            description: str,
            payload: str,
            payment_method: int = 2,
            currency: str = "RUB",
            return_url: str = None,
            failed_url: str = None
    ) -> Optional[Dict[str, Any]]:
        """
        Создать платеж в Platega.io

        payment_method:
        2 - СБП QR
        11 - Карточный эквайринг
        12 - Международный эквайринг
        13 - Криптовалюта
        """

        payload_data = {
            "paymentMethod": payment_method,
            "paymentDetails": {
                "amount": amount,
                "currency": currency
            },
            "description": description[:255],
            "payload": payload[:255]
        }

        if return_url:
            payload_data["return"] = return_url

        if failed_url:
            payload_data["failedUrl"] = failed_url

        logger.info(f"💰 Creating payment: {json.dumps(payload_data, indent=2, ensure_ascii=False)}")

        response = await self._request("POST", "/transaction/process", json=payload_data)

        if response and not response.get("error"):
            logger.info(f"✅ Payment created successfully: {response.get('transactionId')}")
            return response

        logger.error(f"❌ Failed to create payment: {response}")
        return None

    # ======================================================
    # ПРОВЕРКА СТАТУСА ПЛАТЕЖА
    # ======================================================

    async def check_payment_status(self, transaction_id: str) -> Optional[Dict[str, Any]]:
        """Проверить статус платежа по ID транзакции"""
        response = await self._request("GET", f"/transaction/{transaction_id}")

        if response and not response.get("error"):
            return response

        logger.error(f"❌ Failed to check payment status: {response}")
        return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()