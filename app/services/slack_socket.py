import asyncio
import logging
from typing import Any

from app.core.config import settings
from app.core.state import state
from app.services.upbit_client import UpbitAPIError, upbit_client

logger = logging.getLogger(__name__)


class SlackSocketService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._client = None
        self._web_client = None
        self._bot_user_id: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(settings.slack_app_token and settings.slack_bot_token)

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Slack Socket Mode disabled; missing tokens")
            return
        if self._task:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="slack-socket")

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop_event.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
            from slack_sdk.socket_mode.response import SocketModeResponse
            from slack_sdk.web.async_client import AsyncWebClient
        except Exception as exc:
            logger.exception("Slack SDK not available: %s", exc)
            return

        self._web_client = AsyncWebClient(token=settings.slack_bot_token)
        self._client = SocketModeClient(
            app_token=settings.slack_app_token,
            web_client=self._web_client,
        )

        async def _process(client: Any, req: Any) -> None:
            if req.type in ("events_api", "slash_commands", "interactive"):
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id)
                )
            if req.type != "events_api":
                return
            try:
                await self._handle_event(req.payload.get("event") or {})
            except Exception as exc:
                logger.exception("Slack event handling error: %s", exc)

        self._client.socket_mode_request_listeners.append(_process)

        await self._load_bot_user_id()
        try:
            await self._client.connect()
            logger.info("Slack Socket Mode connected")
            await self._stop_event.wait()
        except Exception as exc:
            logger.exception("Slack Socket Mode connection error: %s", exc)
        finally:
            await self._shutdown_client()

    async def _shutdown_client(self) -> None:
        if self._client is not None:
            for method_name in ("close", "disconnect"):
                method = getattr(self._client, method_name, None)
                if method:
                    result = method()
                    if asyncio.iscoroutine(result):
                        await result
                    break
            self._client = None

        if self._web_client is not None:
            close_method = getattr(self._web_client, "close", None)
            if callable(close_method):
                result = close_method()
                if asyncio.iscoroutine(result):
                    await result
            else:
                session = getattr(self._web_client, "session", None)
                if session is not None:
                    await session.close()
            self._web_client = None

    async def _load_bot_user_id(self) -> None:
        if not self._web_client:
            return
        try:
            auth = await self._web_client.auth_test()
            self._bot_user_id = auth.get("user_id")
        except Exception as exc:
            logger.warning("Slack auth_test failed: %s", exc)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        logger.debug(
            "Slack event received: type=%s channel=%s channel_type=%s",
            event_type,
            event.get("channel"),
            event.get("channel_type"),
        )
        if event_type == "app_mention":
            text = self._strip_mention(event.get("text", ""))
            await self._handle_command(text, event)
            return

        if event_type == "message":
            if event.get("bot_id") or event.get("subtype") == "bot_message":
                return
            channel = event.get("channel")
            channel_type = event.get("channel_type")
            if channel_type not in ("im", "mpim"):
                if not (isinstance(channel, str) and channel.startswith(("D", "G"))):
                    return
            text = event.get("text", "")
            await self._handle_command(text, event)
            return

    def _strip_mention(self, text: str) -> str:
        if not self._bot_user_id:
            return text.strip()
        mention = f"<@{self._bot_user_id}>"
        return text.replace(mention, "").strip()

    async def _handle_command(self, text: str, event: dict[str, Any]) -> None:
        channel = event.get("channel")
        if not channel:
            return
        raw = (text or "").strip()
        if not raw:
            await self._send_help(channel)
            return

        cmd = raw.lower()
        if cmd in ("help", "/help", "도움말", "도움"):
            await self._send_help(channel)
            return

        if cmd in ("status", "/status", "상태"):
            await self._send_status(channel)
            return

        if "잔고" in cmd or cmd in ("/balance", "balance"):
            await self._send_balance(channel)
            return

        await self._post_message(channel, "지원하지 않는 명령입니다. 'help'를 입력하세요.")

    async def _send_help(self, channel: str) -> None:
        text = (
            "사용 가능한 명령:\n"
            "- 잔고 / balance\n"
            "- status\n"
            "- help\n"
        )
        await self._post_message(channel, text)

    async def _send_status(self, channel: str) -> None:
        status = state.status()
        heartbeat = status.last_heartbeat or "-"
        err = status.last_error or "-"
        text = (
            f"봇 상태: {'실행 중' if status.running else '중지'}\n"
            f"마지막 하트비트: {heartbeat}\n"
            f"최근 오류: {err}"
        )
        await self._post_message(channel, text)

    async def _send_balance(self, channel: str) -> None:
        if not settings.upbit_access_key or not settings.upbit_secret_key:
            await self._post_message(
                channel,
                "Upbit 키가 설정되지 않았습니다. .env의 UPBIT_ACCESS_KEY/SECRET_KEY를 확인하세요.",
            )
            return

        try:
            accounts = await upbit_client.get_accounts()
        except UpbitAPIError as exc:
            payload = exc.to_dict()
            await self._post_message(
                channel,
                f"Upbit 오류: {payload.get('error_name')} {payload.get('message')}",
            )
            return

        balances = self._extract_balances(accounts)
        if not balances:
            await self._post_message(channel, "표시할 잔고가 없습니다.")
            return

        price_map = await self._load_prices(balances)
        lines = self._format_balances(balances, price_map)
        await self._post_message(channel, "\n".join(lines))

    def _extract_balances(self, accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for item in accounts:
            currency = item.get("currency")
            balance = self._to_float(item.get("balance"))
            locked = self._to_float(item.get("locked"))
            total = balance + locked
            if not currency or total <= 0:
                continue
            results.append(
                {
                    "currency": currency,
                    "balance": balance,
                    "locked": locked,
                    "total": total,
                    "avg_buy_price": self._to_float(item.get("avg_buy_price")),
                    "unit_currency": item.get("unit_currency") or "KRW",
                }
            )
        return results

    async def _load_prices(self, balances: list[dict[str, Any]]) -> dict[str, float]:
        markets = [f"KRW-{b['currency']}" for b in balances if b["currency"] != "KRW"]
        if not markets:
            return {}
        try:
            tickers = await upbit_client.get_ticker(markets)
        except UpbitAPIError as exc:
            logger.warning("Upbit ticker error: %s", exc)
            return {}
        price_map: dict[str, float] = {}
        for item in tickers:
            market = item.get("market")
            price = item.get("trade_price")
            if market and price is not None:
                price_map[market] = float(price)
        return price_map

    def _format_balances(
        self,
        balances: list[dict[str, Any]],
        price_map: dict[str, float],
    ) -> list[str]:
        summary_lines = ["[잔고 요약]"]
        detail_lines = ["[보유 코인]"]
        unknown_symbols: list[str] = []

        krw_balance = 0.0
        krw_locked = 0.0
        coin_value = 0.0

        for item in balances:
            currency = item["currency"]
            total = item["total"]
            locked = item["locked"]
            avg_buy = item["avg_buy_price"]
            unit_currency = item["unit_currency"]

            if currency == "KRW":
                krw_balance = item["balance"]
                krw_locked = locked
                continue

            line = f"{currency}: 수량 {self._fmt_amount(total)}"
            if locked > 0:
                line += f" (주문중 {self._fmt_amount(locked)})"
            if avg_buy > 0:
                line += f" | 평균단가 {self._fmt_krw(avg_buy)} {unit_currency}"
            else:
                line += " | 평균단가 -"

            market = f"KRW-{currency}"
            price = price_map.get(market)
            if price:
                value = price * total
                coin_value += value
                line += f" | 추정 {self._fmt_krw(value)} KRW"
            else:
                unknown_symbols.append(currency)
                line += " | 추정 -"

            detail_lines.append(line)

        krw_total = krw_balance + krw_locked
        summary_line = f"계좌 잔고(KRW): {self._fmt_krw(krw_total)} KRW"
        if krw_locked > 0:
            summary_line += f" (주문중 {self._fmt_krw(krw_locked)} KRW)"
        summary_lines.append(summary_line)
        summary_lines.append(f"보유 코인 평가액: {self._fmt_krw(coin_value)} KRW")
        summary_lines.append(f"추정 총자산: {self._fmt_krw(krw_total + coin_value)} KRW")
        if unknown_symbols:
            summary_lines.append(f"미시세 코인: {', '.join(sorted(unknown_symbols))}")

        if len(detail_lines) == 1:
            detail_lines.append("보유 코인이 없습니다.")

        return summary_lines + [""] + detail_lines

    async def _post_message(self, channel: str, text: str) -> None:
        if not self._web_client:
            logger.warning("Slack web client not ready")
            return
        await self._web_client.chat_postMessage(channel=channel, text=text)

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _fmt_amount(value: float, decimals: int = 8) -> str:
        formatted = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
        return formatted or "0"

    @staticmethod
    def _fmt_krw(value: float) -> str:
        return f"{value:,.0f}"


slack_socket_service = SlackSocketService()
