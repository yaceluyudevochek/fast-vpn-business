import aiohttp
import logging
import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


class PanelAPI:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self._session = None
        self.api_url = f"{self.base_url}/api"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _prepare_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

    async def _request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        session = await self._get_session()
        headers = await self._prepare_headers()
        url = f"{self.api_url}/{endpoint.lstrip('/')}"

        try:
            async with session.request(method, url, headers=headers, **kwargs) as response:
                response_text = await response.text()

                if response.status in [200, 201, 202, 204]:
                    return json.loads(response_text) if response_text else {}

                logger.error(f"API error {response.status}: {response_text}")
                return {"error": True, "status": response.status, "details": response_text}

        except Exception as e:
            logger.error(f"Request error: {e}")
            return None

    # ======================================================
    # USERS
    # ======================================================

    async def update_user_status_by_username(self, username: str, status: str) -> bool:
        """Update user status by username"""
        user = await self.get_user_by_username(username)
        if not user:
            logger.error(f"User {username} not found in panel")
            return False

        payload = {
            "username": username,
            "status": status
        }

        response = await self._request("PATCH", "users", json=payload)

        if response is not None and not response.get("error"):
            logger.info(f"User {username} status updated to {status}")
            return True

        return False

    async def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user by username"""
        response = await self._request("GET", f"users/by-username/{username}")

        if response and not response.get("error") and "response" in response:
            return response["response"]

        return None

    async def get_user_by_telegram_id(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        """Get user by telegram ID (для обратной совместимости)"""
        response = await self._request("GET", f"users/by-telegram-id/{telegram_id}")

        if response and not response.get("error") and "response" in response:
            users = response["response"]

            if isinstance(users, list) and users:
                return users[0]

            if isinstance(users, dict):
                return users

        return None

    async def create_user(
            self,
            username: str,
            telegram_id: int,
            squad_id: str,
            days: int = 30,
            hwid_limit: int = 3,
            tag: str = None,
            traffic_limit_bytes: int = 0  # новый параметр, 0 = безлимит
    ) -> Optional[Dict[str, Any]]:
        """Create a new user with specific squad"""
        expire_at = (
                datetime.now(timezone.utc) + timedelta(days=days)
        ).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

        if not tag:
            tag = f"U{telegram_id}"
            if len(tag) > 16:
                tag = tag[:16]

        payload = {
            "username": username,
            "telegramId": telegram_id,
            "status": "ACTIVE",
            "expireAt": expire_at,
            "trafficLimitBytes": traffic_limit_bytes,  # используем лимит из плана
            "trafficLimitStrategy": "NO_RESET" if traffic_limit_bytes == 0 else "MONTH",
            # если есть лимит - сбрасываем monthly
            "hwidDeviceLimit": hwid_limit,
            "tag": tag,
            "activeInternalSquads": [squad_id]
        }

        logger.info(f"Creating user with payload: {payload}")

        response = await self._request("POST", "users", json=payload)

        if response and not response.get("error") and "response" in response:
            return response["response"]

        logger.error(f"Failed to create user: {response}")
        return None

    async def update_user_status(self, username: str, status: str) -> bool:
        """Update user status by username"""
        user = await self.get_user_by_username(username)
        if not user:
            logger.error(f"User {username} not found in panel")
            return False

        payload = {
            "username": username,
            "status": status
        }

        response = await self._request("PATCH", "users", json=payload)

        if response is not None and not response.get("error"):
            logger.info(f"User {username} status updated to {status}")
            return True

        return False

    async def extend_subscription_by_username(self, username: str, days: int, traffic_limit_bytes: int = None) -> \
    Optional[str]:
        """Extend user subscription by username with optional traffic limit update"""
        user = await self.get_user_by_username(username)

        if not user:
            return None

        current_expire = user.get("expireAt")

        if current_expire:
            expire_dt = datetime.fromisoformat(current_expire.replace('Z', '+00:00'))
        else:
            expire_dt = datetime.now(timezone.utc)

        now = datetime.now(timezone.utc)
        start_dt = expire_dt if expire_dt > now else now
        new_expire_dt = start_dt + timedelta(days=days)

        new_expire_str = new_expire_dt.isoformat(
            timespec='milliseconds'
        ).replace('+00:00', 'Z')

        payload = {
            "username": username,
            "expireAt": new_expire_str,
            "status": "ACTIVE"
        }

        # Если передан новый лимит трафика, добавляем его в payload
        if traffic_limit_bytes is not None:
            payload["trafficLimitBytes"] = traffic_limit_bytes

        response = await self._request("PATCH", "users", json=payload)

        if response is not None and not response.get("error"):
            return new_expire_str

        return None

    # ======================================================
    # SUBSCRIPTION INFO
    # ======================================================

    async def get_subscription_info_by_short_uuid(self, short_uuid: str) -> Optional[Dict[str, Any]]:
        """Get subscription info by short UUID"""
        response = await self._request("GET", f"sub/{short_uuid}/info")

        if response and not response.get("error") and "response" in response:
            return response["response"]

        return None

    async def format_subscription_info_by_short_uuid(self, short_uuid: str) -> Optional[Dict[str, Any]]:
        """Format subscription info for display"""
        subscription = await self.get_subscription_info_by_short_uuid(short_uuid)

        if not subscription:
            return None

        result = {
            "is_active": False,
            "days_left": 0,
            "expiry_date": None,
            "formatted_expiry": "Неизвестно",
            "status": "DISABLED"
        }

        if "user" in subscription:
            user_data = subscription["user"]

            result["days_left"] = user_data.get("daysLeft", 0)
            result["status"] = user_data.get("userStatus", "DISABLED")
            result["is_active"] = user_data.get("isActive", False)

            expiry = user_data.get("expiresAt")
            if expiry:
                try:
                    date_obj = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                    msk_time = date_obj + timedelta(hours=3)
                    result["expiry_date"] = expiry
                    result["formatted_expiry"] = msk_time.strftime("%d.%m.%Y %H:%M МСК")
                except Exception as e:
                    logger.error(f"Error parsing expiry: {e}")
                    result["formatted_expiry"] = expiry

        return result

    # ======================================================
    # HWID DEVICES
    # ======================================================

    async def get_user_hwid_devices(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get user HWID devices"""
        response = await self._request("GET", f"hwid/devices/{user_id}")

        if response and not response.get("error") and "response" in response:
            return response["response"]

        return None

    async def get_user_hwid_devices_count_by_username(self, username: str) -> int:
        """Get count of active HWID devices for user by username"""
        user = await self.get_user_by_username(username)

        if not user:
            return 0

        user_id = user.get("id")
        if not user_id:
            return 0

        devices_data = await self.get_user_hwid_devices(user_id)

        if devices_data and "devices" in devices_data:
            return len(devices_data["devices"])

        return 0

    async def reset_user_hwid_devices_by_username(self, username: str) -> bool:
        """Reset all HWID devices for user by username"""
        user = await self.get_user_by_username(username)

        if not user:
            return False

        user_id = user.get("id")
        if not user_id:
            return False

        payload = {"userId": user_id}
        response = await self._request("POST", "hwid/devices/delete-all", json=payload)

        if response and not response.get("error"):
            logger.info(f"Successfully reset HWID devices for user {username}")
            return True

        return False

    async def close(self):
        if self._session:
            await self._session.close()

    # ======================================================
    # УМЕНЬШЕНИЕ ДНЕЙ ПОДПИСКИ (новый метод)
    # ======================================================

    async def reduce_subscription_days_by_username(self, username: str, days_to_reduce: int) -> Optional[str]:
        """
        Уменьшить количество дней подписки пользователя
        Возвращает новую дату окончания или None в случае ошибки
        """
        user = await self.get_user_by_username(username)

        if not user:
            logger.error(f"User {username} not found in panel")
            return None

        current_expire = user.get("expireAt")

        if not current_expire:
            logger.error(f"User {username} has no expiry date")
            return None

        # Парсим текущую дату окончания
        expire_dt = datetime.fromisoformat(current_expire.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)

        # Если подписка уже истекла, не уменьшаем
        if expire_dt <= now:
            logger.error(f"User {username} subscription already expired")
            return None

        # Вычисляем новую дату окончания
        new_expire_dt = expire_dt - timedelta(days=days_to_reduce)

        # ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА: новая дата не должна быть меньше текущей
        if new_expire_dt <= now:
            # Вместо автоматической корректировки - возвращаем ошибку
            logger.error(f"Cannot reduce by {days_to_reduce} days - would result in past date")
            return None

        new_expire_str = new_expire_dt.isoformat(
            timespec='milliseconds'
        ).replace('+00:00', 'Z')

        payload = {
            "username": username,
            "expireAt": new_expire_str,
            "status": "ACTIVE"
        }

        response = await self._request("PATCH", "users", json=payload)

        if response is not None and not response.get("error"):
            logger.info(f"✅ Reduced {days_to_reduce} days for user {username}. New expiry: {new_expire_str}")
            return new_expire_str

        logger.error(f"❌ Failed to reduce days for user {username}: {response}")
        return None

    async def reduce_subscription_days_by_subscription_id(self, subscription_id: int, days_to_reduce: int) -> Optional[
        str]:
        """
        Уменьшить количество дней подписки по ID подписки в БД
        """
        from database import db as database

        subscription = await database.get_subscription(subscription_id)
        if not subscription:
            logger.error(f"Subscription {subscription_id} not found in database")
            return None

        return await self.reduce_subscription_days_by_username(
            subscription['username_in_panel'],
            days_to_reduce
        )

    # ======================================================
    # СБРОС СЧЕТЧИКА ТРАФИКА У ПОЛЬЗОВАТЕЛЯ
    # ======================================================
    async def reset_user_traffic(self, username: str) -> bool:
        """
        Сбросить счетчик использованного трафика для пользователя
        POST /api/users/{id}/actions/reset-traffic
        """
        user = await self.get_user_by_username(username)
        if not user:
            logger.error(f"❌ User {username} not found in panel")
            return False

        user_id = user.get("id")
        if not user_id:
            logger.error(f"❌ User {username} has no ID")
            return False

        endpoint = f"users/{user_id}/actions/reset-traffic"

        logger.info(f"🔄 Resetting traffic for user {username} (ID: {user_id})")

        # Сохраняем старые значения для сравнения
        old_traffic = user.get("userTraffic", {}).get("usedTrafficBytes", 0)
        old_limit = user.get("trafficLimitBytes", 0)

        logger.info(f"   Before reset: used={old_traffic} bytes, limit={old_limit} bytes")

        response = await self._request("POST", endpoint)

        if response is not None and not response.get("error"):
            # Проверяем разные форматы ответа
            if "response" in response:
                user_data = response["response"]
                new_traffic = user_data.get("userTraffic", {}).get("usedTrafficBytes", 0)
                logger.info(f"✅ Traffic reset successful for {username}")
                logger.info(f"   After reset: used={new_traffic} bytes")

                # Проверяем, что сброс действительно произошел
                if new_traffic == 0:
                    logger.info(f"   ✓ Traffic counter successfully reset to 0")
                    return True
                elif new_traffic < old_traffic:
                    logger.info(f"   ✓ Traffic decreased from {old_traffic} to {new_traffic}")
                    return True
                else:
                    logger.warning(f"   ⚠️ Traffic did not decrease as expected")
                    # Все равно возвращаем True, так как API ответил успешно
                    return True
            else:
                # Если ответ в другом формате, но нет ошибки - считаем успехом
                logger.info(f"✅ Traffic reset successful for {username} (unexpected response format)")
                logger.debug(f"   Response: {response}")
                return True

        # Детальный разбор ошибки
        error_msg = "Unknown error"
        if response:
            if response.get("status") == 404:
                error_msg = f"User {username} (ID: {user_id}) not found"
            elif response.get("status") == 400:
                error_msg = f"Bad request: {response.get('details', 'No details')}"
            elif response.get("status") == 500:
                error_msg = f"Server error: {response.get('details', 'No details')}"
            else:
                error_msg = str(response)

        logger.error(f"❌ Failed to reset traffic for {username}: {error_msg}")
        return False