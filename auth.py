"""Interactive one-time Telegram login helper (QR code or phone) for Telethon."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import io
import sys
import webbrowser
from pathlib import Path

import qrcode
import qrcode.image.svg
from telethon import errors

from client import build_telethon_client, entity_display_name
from config import (
    PROJECT_DIR,
    ConfigError,
    TelegramConfig,
    load_config,
    save_env_credentials,
)

QR_HTML_PATH = PROJECT_DIR / "qr_login.html"
MAX_QR_REFRESHES = 6
MAX_2FA_ATTEMPTS = 3


def _configure_utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _prompt_credentials() -> TelegramConfig:
    print("Telegram API credentials were not found in .env or environment.")
    print("Get your api_id and api_hash at https://my.telegram.org/apps\n")

    while True:
        raw_id = input("Enter TELEGRAM_API_ID: ").strip()
        try:
            api_id = int(raw_id)
            if api_id > 0:
                break
        except ValueError:
            pass
        print("Invalid api_id. Please enter a positive integer.")

    while True:
        api_hash = input("Enter TELEGRAM_API_HASH: ").strip()
        if api_hash and "\n" not in api_hash and "\r" not in api_hash:
            break
        print("Invalid api_hash. Please enter a non-empty string.")

    env_path = save_env_credentials(api_id, api_hash)
    print(f"Saved credentials to {env_path}\n")
    return load_config(env_path)


def render_qr_login_html(url: str, output_path: Path = QR_HTML_PATH) -> tuple[str, Path]:
    """Generate a terminal ASCII QR code and a crisp auto-refreshing HTML page."""
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)

    ascii_buf = io.StringIO()
    qr.print_ascii(out=ascii_buf, invert=True)

    svg_buf = io.BytesIO()
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    img.save(svg_buf)
    svg_markup = svg_buf.getvalue().decode("utf-8")

    html = (
        "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='5'>"
        "<title>Telegram QR Login</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;display:flex;flex-direction:column;"
        "align-items:center;justify-content:center;min-height:100vh;margin:0;"
        "background:#f5f7fa;color:#111827;}"
        ".card{background:#fff;padding:32px;border-radius:16px;"
        "box-shadow:0 8px 24px rgba(0,0,0,.08);text-align:center;max-width:420px;}"
        ".qr svg{width:280px;height:280px;display:block;margin:16px auto;}"
        "p{color:#4b5563;line-height:1.5;font-size:15px;}"
        "</style></head><body><div class='card'>"
        "<h2>Вход в Telegram по QR-коду</h2>"
        f"<div class='qr'>{svg_markup}</div>"
        "<p>Откройте Telegram на телефоне:<br>"
        "<b>Настройки → Устройства → Подключить устройство</b><br>"
        "и отсканируйте этот QR-код.</p>"
        "<p style='font-size:12px;color:#6b7280'>Страница обновляется автоматически; "
        "если включён облачный пароль (2FA), введите его в окне PowerShell.</p>"
        "</div></body></html>"
    )
    output_path.write_text(html, encoding="utf-8")
    return ascii_buf.getvalue(), output_path


async def _finish_2fa_login(client: object) -> None:
    for attempt in range(1, MAX_2FA_ATTEMPTS + 1):
        password = getpass.getpass("Введите облачный пароль (2FA): ")
        try:
            await client.sign_in(password=password)
            return
        except errors.PasswordHashInvalidError:
            remaining = MAX_2FA_ATTEMPTS - attempt
            if remaining <= 0:
                raise
            print(f"Неверный пароль. Осталось попыток: {remaining}")


async def _login_with_qr(client: object) -> None:
    print("\nОткройте Telegram на телефоне:")
    print("  Настройки -> Устройства -> Подключить устройство")
    print("Сейчас откроется страница с QR-кодом в браузере (и копия в терминале).\n")

    qr_login = await client.qr_login()
    opened_browser = False
    try:
        for refresh_idx in range(MAX_QR_REFRESHES):
            ascii_qr, html_path = render_qr_login_html(qr_login.url)
            print(ascii_qr)
            if not opened_browser:
                opened_browser = True
                try:
                    webbrowser.open(html_path.as_uri())
                except Exception:
                    print(f"Откройте вручную файл в браузере: {html_path}")
            else:
                print("QR-код обновлён (страница в браузере обновится сама через пару секунд).")

            try:
                await qr_login.wait()
                return
            except asyncio.TimeoutError:
                if refresh_idx + 1 >= MAX_QR_REFRESHES:
                    raise TimeoutError(
                        "Время ожидания сканирования QR-кода истекло. Запустите auth.py снова."
                    )
                print("\nСрок действия QR-кода истёк, генерирую новый...\n")
                await qr_login.recreate()
            except errors.SessionPasswordNeededError:
                print("\nQR-код подтверждён. Требуется облачный пароль (2FA).")
                await _finish_2fa_login(client)
                return
    finally:
        try:
            if QR_HTML_PATH.exists():
                QR_HTML_PATH.unlink()
        except OSError:
            pass


async def _login_with_phone(client: object) -> None:
    print(
        "\nПодсказка: Telegram обычно присылает код НЕ по SMS, "
        "а в сервисный чат 'Telegram' внутри уже открытого приложения.\n"
    )
    await client.start()


def _choose_method(cli_mode: str | None) -> str:
    if cli_mode in ("qr", "phone"):
        return cli_mode
    print("Выберите способ входа:")
    print("  [1] QR-код (рекомендуется — без кода/SMS)")
    print("  [2] Номер телефона + код подтверждения")
    choice = input("Ваш выбор [1]: ").strip()
    return "phone" if choice == "2" else "qr"


async def _run_auth(
    cli_mode: str | None = None,
    session_override: str | None = None,
) -> int:
    _configure_utf8_stdout()
    try:
        cfg = load_config(session_override=session_override)
    except ConfigError:
        cfg = _prompt_credentials()
        if session_override is not None:
            cfg = load_config(session_override=session_override)

    client = build_telethon_client(cfg)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            method = _choose_method(cli_mode)
            if method == "phone":
                await _login_with_phone(client)
            else:
                await _login_with_qr(client)
        me = await client.get_me()
    finally:
        await client.disconnect()

    username = getattr(me, "username", None)
    handle = f" (@{username})" if username else ""
    print(
        f"\nAuthorized as {entity_display_name(me)}{handle} [id={getattr(me, 'id', 'unknown')}]"
    )
    print(f"Session stored at: {cfg.session_path}")
    print("You can now reload/enable the 'telegram' MCP server in Antigravity.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run interactive Telegram authentication."""
    parser = argparse.ArgumentParser(description="Authorize local Telegram MCP session.")
    parser.add_argument(
        "--session",
        default=None,
        help="Optional custom session file name or path (e.g. telegram_autoreply.session)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--qr", action="store_true", help="Sign in using QR code")
    group.add_argument("--phone", action="store_true", help="Sign in using phone + code")
    args = parser.parse_args(argv)
    mode = "qr" if args.qr else ("phone" if args.phone else None)

    try:
        return asyncio.run(_run_auth(mode, session_override=args.session))
    except (KeyboardInterrupt, EOFError):
        print("\nAuthentication cancelled.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nAuthentication error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
