"""
Qoder Creator - Signup Manager
Full signup flow: temp mail → form → captcha (local slider) → OTP → account created → PAT.
"""

import asyncio
import random
import time
from typing import Optional, Dict, Any

from playwright.async_api import async_playwright, Page
from rich.console import Console
from rich.text import Text

from .config import QODER_BASE, ACCOUNTS_FILE
from .utils import write_log, generate_password, save_jsonl
from .tempmail import TempikClient
from .proxy import ProxyPool
from .stealth import create_stealth_context, launch_stealth_browser
from .captcha import solve_slider_local
from .pat import PATManager

import os

SHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug_shots")
os.makedirs(SHOT_DIR, exist_ok=True)


async def _shot(page, name: str):
    """Save a full-page screenshot + dump HTML for debugging."""
    try:
        await page.screenshot(path=os.path.join(SHOT_DIR, f"{name}.png"), full_page=True)
        with open(os.path.join(SHOT_DIR, f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(await page.content())
        write_log(f"screenshot: {name} url={page.url}", "INFO")
    except Exception as e:
        write_log(f"screenshot fail {name}: {e}", "ERROR")


class SignupManager:
    """Handle Qoder signup + PAT creation."""

    def __init__(
        self,
        proxy_pool: ProxyPool = None,
        headless: bool = True,
        console: Console = None,
    ):
        self.proxy_pool = proxy_pool
        self.headless = headless
        self.console = console or Console()

    async def create_account(
        self,
        idx: int = 0,
        proxy: Dict[str, str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Full signup flow for one account.
        Returns: {email, password, pat, ...} or None on failure.
        """
        if proxy is None and self.proxy_pool:
            proxy = self.proxy_pool.next()

        c = self.console

        # Step 1: Get temp email
        c.print(f"\n[bold cyan]Account #{idx}[/]")
        c.print("  :mailbox: Getting temp email...")
        tempmail = TempikClient()
        email = tempmail.create_inbox()
        password = generate_password()

        c.print(f"  :envelope: Email: [cyan]{email}[/]")
        c.print(f"  :key: Password: [yellow]{password}[/]")
        write_log(f"[{idx}] Signup start: {email}", "INFO")

        try:
            async with async_playwright() as p:
                # Step 2: Launch browser
                c.print("  :rocket: Launching browser...")
                browser = await launch_stealth_browser(p, proxy, self.headless)
                context = await create_stealth_context(browser, proxy)
                page = await context.new_page()

                # Step 3: Open signup page
                c.print("  :globe_with_meridians: Opening signup page...")
                await page.goto(
                    f"{QODER_BASE}/users/sign-up",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                await page.wait_for_timeout(5000)

                # Step 4: Fill form (name + email)
                c.print("  :pencil: Filling signup form...")
                try:
                    await page.fill("#basic_firstName", "User")
                    await page.fill("#basic_lastName", "Dev")
                    await page.fill("#basic_email", email)
                except Exception as e:
                    write_log(f"[{idx}] Form fill error: {e}", "ERROR")
                    c.print(f"  :x: [red]Form fill failed: {e}[/]")
                    await context.close()
                    await browser.close()
                    return None

                # Checkbox
                cb = await page.query_selector("input[type=checkbox]")
                if cb:
                    try:
                        await cb.check(force=True)
                    except Exception:
                        pass

                # Click Continue
                await self._click(page, ['button:has-text("Continue")'])
                await page.wait_for_timeout(4000)

                # Step 5: Fill password
                c.print("  :lock: Filling password...")
                pw = await page.query_selector("#basic_password")
                if pw:
                    try:
                        await pw.click(force=True)
                        await page.keyboard.type(password, delay=20)
                    except Exception:
                        pass
                await self._click(page, ['button:has-text("Continue")'])
                await page.wait_for_timeout(4000)

                # Step 6: Solve captcha
                c.print("  :puzzle_piece: Solving captcha...")
                await self._click(page, [
                    "#aliyunCaptcha-captcha-body",
                    'button:has-text("Click to verify")',
                ])
                await page.wait_for_timeout(3000)

                solved = await solve_slider_local(page, max_attempts=5, console=c)
                if not solved:
                    c.print("  :x: [red]Captcha failed![/]")
                    write_log(f"[{idx}] Captcha failed for {email}", "ERROR")
                    await context.close()
                    await browser.close()
                    return None

                c.print("  :white_check_mark: [green]Captcha solved![/]")
                await page.wait_for_timeout(2000)

                # Step 7: Wait for OTP
                c.print("  :inbox_tray: Waiting for OTP email...")
                messages = await tempmail.wait_for_messages(email, max_wait=150, interval=5)

                if not messages:
                    c.print("  :x: [red]No messages received — OTP timeout![/]")
                    write_log(f"[{idx}] OTP timeout for {email}", "ERROR")
                    await context.close()
                    await browser.close()
                    return None

                otp = tempmail.extract_otp(messages)
                if not otp:
                    c.print("  :x: [red]Could not extract OTP![/]")
                    write_log(f"[{idx}] OTP extraction failed for {email}", "ERROR")
                    await context.close()
                    await browser.close()
                    return None

                c.print(f"  :incoming_envelope: OTP: [bold green]{otp}[/]")

                # Debug: snapshot before filling OTP
                await _shot(page, "01_before_otp")

                # Step 8: Fill OTP
                c.print("  :keyboard: Filling OTP...")
                otp_inputs = await page.query_selector_all('input.ant-otp-input')
                if len(otp_inputs) >= 6:
                    await otp_inputs[0].click()
                    await page.wait_for_timeout(200)
                    await page.keyboard.type(otp, delay=80)
                    await page.wait_for_timeout(1500)
                else:
                    all_inputs = await page.query_selector_all('input:not([type="hidden"])')
                    if all_inputs:
                        await all_inputs[0].click()
                        await page.keyboard.type(otp, delay=80)
                    else:
                        await page.keyboard.type(otp, delay=80)

                # Debug: snapshot right after typing OTP
                await _shot(page, "02_after_typing_otp")

                # --- Robust OTP submit -------------------------------------
                # Qoder's code field auto-submits once the last digit is typed,
                # but this is flaky. Try several strategies until the page
                # actually leaves the sign-up page.
                def _left_signup(u: str) -> bool:
                    return "sign-up" not in u and ("download" in u or "dashboard" in u or "workspace" in u)

                async def _try_submit_otp():
                    # Strategy 1: explicit button (if present)
                    await self._click(page, [
                        'button:has-text("Create account")',
                        'button:has-text("Verify")',
                        'button:has-text("Continue")',
                        'button[type="submit"]',
                    ])
                    await page.wait_for_timeout(2500)
                    if _left_signup(page.url):
                        return True
                    # Strategy 2: press Enter on the code field
                    try:
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass
                    await page.wait_for_timeout(2500)
                    if _left_signup(page.url):
                        return True
                    # Strategy 3: clear + retype slowly + Enter
                    try:
                        inp = await page.query_selector('input[autocomplete="one-time-code"], input[inputmode="numeric"], input[maxlength="6"]')
                        if inp:
                            await inp.click()
                            await page.keyboard.press("Control+A")
                            await page.keyboard.press("Backspace")
                            await page.wait_for_timeout(300)
                            await page.keyboard.type(otp, delay=150)
                            await page.wait_for_timeout(1500)
                            await page.keyboard.press("Enter")
                    except Exception:
                        pass
                    await page.wait_for_timeout(2500)
                    return _left_signup(page.url)

                current_url = page.url
                for s in range(1, 4):
                    ok_submit = await _try_submit_otp()
                    current_url = page.url
                    write_log(f"OTP submit strategy {s}: url={current_url} left={_left_signup(current_url)}", "INFO")
                    if _left_signup(current_url):
                        break

                # Extra poll: give the site a moment to finish the redirect
                for _ in range(6):
                    if _left_signup(page.url):
                        break
                    await page.wait_for_timeout(2000)
                current_url = page.url

                # Debug: snapshot after attempting submit
                await _shot(page, "03_after_verify")

                # Step 9: Check result
                if _left_signup(current_url):
                    c.print("  :white_check_mark: [bold green]Account created![/]")
                else:
                    c.print("  :warning: [yellow]Account may be pending... (url={current_url})[/]")

                # Debug: dump all visible buttons + inputs to the log
                try:
                    btns = await page.evaluate(
                        "() => Array.from(document.querySelectorAll('button, a[role=button], .ant-btn'))"
                        ".map(b => (b.innerText||'').trim()).filter(Boolean)"
                    )
                    write_log(f"after-verify buttons={btns}", "INFO")
                    c.print(f"  :label: buttons={btns}")
                except Exception as e:
                    write_log(f"button dump failed: {e}", "WARNING")

                # Step 9b: If still on signup, try navigating to dashboard to force auth
                if not ("download" in current_url or "dashboard" in current_url):
                    c.print("  :arrows_counterclockwise: Trying to force session via dashboard...")
                    try:
                        await page.goto(f"{QODER_BASE}/users/sign-in", wait_until="domcontentloaded", timeout=60000)
                        await page.wait_for_timeout(5000)
                        await _shot(page, "04_signin_page")
                        write_log(f"after signin nav url={page.url}", "INFO")
                    except Exception as e:
                        write_log(f"force session nav failed: {e}", "WARNING")

                # Step 10: Create PAT
                c.print("  :shield: Creating PAT...")
                pat_response = await PATManager.create(page, "farm")
                pat_token = PATManager.extract_token(pat_response)
                pat_valid = PATManager.is_valid(pat_response)

                if pat_valid:
                    c.print(f"  :white_check_mark: [green]PAT valid! ({len(pat_token)} chars)[/]")
                else:
                    c.print(f"  :x: [yellow]PAT invalid: {pat_response.get('status')}[/]")

                await context.close()
                await browser.close()

                # Step 11: Build result
                result = {
                    "email": email,
                    "password": password,
                    "pat_token": pat_token,
                    "pat_valid": pat_valid,
                    "pat_response": pat_response,
                    "url": current_url,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }

                save_jsonl(ACCOUNTS_FILE, result)
                write_log(f"[{idx}] Signup complete: {email} (PAT valid={pat_valid})", "SUCCESS")

                return result

        except Exception as e:
            write_log(f"[{idx}] Signup error: {e}", "ERROR")
            c.print(f"  :x: [red]Error: {e}[/]")
            import traceback
            traceback.print_exc()
            return None

    @staticmethod
    async def _click(page: Page, selectors: list) -> Optional[str]:
        """Click first visible selector."""
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    return sel
            except Exception:
                continue
        return None

    @staticmethod
    async def _shot(page: Page, name: str, console: Console = None) -> None:
        """Save a screenshot + visible button texts for debugging."""
        import os
        try:
            outdir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "shots")
            os.makedirs(outdir, exist_ok=True)
            path = os.path.join(outdir, f"{name}.png")
            await page.screenshot(path=path, full_page=True)
            buttons = await page.evaluate(
                """() => Array.from(document.querySelectorAll('button, a[role=button], .ant-btn'))
                    .map(b => (b.innerText || '').trim()).filter(Boolean)"""
            )
            write_log(f"SHOT {name}: url={page.url} buttons={buttons}", "INFO")
            if console:
                console.print(f"  :camera: shot={name} url={page.url}")
                console.print(f"  :label: buttons={buttons}")
        except Exception as e:
            write_log(f"SHOT {name} failed: {e}", "WARNING")


async def create_accounts(
    count: int = 1,
    proxy_pool: ProxyPool = None,
    headless: bool = True,
    delay: int = 5,
    console: Console = None,
) -> list:
    """Create multiple accounts (legacy — use SignupManager directly)."""
    manager = SignupManager(proxy_pool, headless, console)
    results = []

    for i in range(count):
        result = await manager.create_account(i + 1)
        if result:
            results.append(result)

        if i < count - 1:
            wait = delay + random.randint(0, 5)
            c = console or Console()
            c.print(f"  :hourglass: Waiting [yellow]{wait}s[/] before next account...")
            await asyncio.sleep(wait)

    ok = [r for r in results if r and r.get("pat_valid")]
    c = console or Console()
    c.print()
    if ok:
        c.print(f"[green]:tada: Done: {len(ok)}/{count} accounts with valid PAT[/]")
    else:
        c.print(f"[red]:x: All {count} accounts failed[/]")
    c.print(f"[dim]Saved to: {ACCOUNTS_FILE}[/]")

    return results