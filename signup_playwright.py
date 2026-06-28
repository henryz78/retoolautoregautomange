import asyncio
import json
import os
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


BASE = "https://login.retool.com"
SIGNUP_URL = f"{BASE}/auth/signup?source=navbarcta"
FOLLOWUP_URL_PART = "/auth/followup"
CLIENT_VERSION = "4.14.0-59bdefe (Build 351982)"
CHROME_PATH = os.getenv(
    "CHROME_PATH",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)

EMAIL = "your-email@example.com"
PASSWORD = "your-password"
FIRST_NAME = "coftens"
LAST_NAME = ""
SUBDOMAIN = "coftens"


async def response_body(resp) -> str:
    try:
        return await resp.text()
    except Exception as exc:
        return f"<failed to read response: {exc}>"


def pretty_text(text: str) -> str:
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except Exception:
        return text[:800]


async def wait_for_signup_form(page) -> None:
    email_locator = page.locator('input[name="email"], input[placeholder="Work email"]')
    for _ in range(30):
        if await email_locator.count() > 0 and await email_locator.first.is_visible():
            return
        title = await page.title()
        if "Just a moment" in title:
            print("等待 Cloudflare 挑战通过...")
        await page.wait_for_timeout(2000)
    raise RuntimeError("60 秒内未出现注册表单，页面可能仍卡在 Cloudflare 或风控")


async def fill_signup_form(page) -> None:
    email_locator = page.locator('input[name="email"], input[placeholder="Work email"]').first
    password_locator = page.locator('input[name="password"], input[placeholder="Password"], input[type="password"]').first
    await email_locator.fill(EMAIL)
    await password_locator.fill(PASSWORD)


async def fill_followup_form(page) -> None:
    text_inputs = page.locator("input[type='text']")
    count = await text_inputs.count()
    if count < 2:
        raise RuntimeError("followup 页面未找到足够的文本输入框")
    await text_inputs.nth(0).fill(FIRST_NAME)
    await text_inputs.nth(1).fill(SUBDOMAIN)


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            executable_path=CHROME_PATH,
        )
        context = await browser.new_context()
        page = await context.new_page()

        try:
            print(f"打开: {SIGNUP_URL}")
            await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
            await wait_for_signup_form(page)
            print("注册表单已出现")

            await fill_signup_form(page)

            continue_button = page.get_by_role("button", name="Continue")
            if await continue_button.count() == 0:
                raise RuntimeError("未找到注册页 Continue 按钮")

            async with page.expect_response(
                lambda resp: "/api/signup" in resp.url and resp.request.method == "POST",
                timeout=60000,
            ) as signup_info:
                await continue_button.click()

            signup_resp = await signup_info.value
            signup_body = await response_body(signup_resp)
            print("\nPOST /api/signup")
            print("status:", signup_resp.status)
            print(pretty_text(signup_body))

            if signup_resp.status >= 400:
                raise RuntimeError("signup 接口返回失败")

            await page.wait_for_url(f"**{FOLLOWUP_URL_PART}**", timeout=60000)
            print("\nfollowup url:", page.url)

            await fill_followup_form(page)

            followup_button = page.get_by_role("button", name="Continue")
            if await followup_button.count() == 0:
                raise RuntimeError("未找到 followup Continue 按钮")

            change_name_waiter = page.wait_for_response(
                lambda resp: "/api/user/changeName" in resp.url and resp.request.method == "POST",
                timeout=60000,
            )
            init_org_waiter = page.wait_for_response(
                lambda resp: "/api/organization/admin/initializeOrganization" in resp.url and resp.request.method == "POST",
                timeout=60000,
            )

            await followup_button.click()

            change_name_resp = await change_name_waiter
            init_org_resp = await init_org_waiter

            change_name_body = await response_body(change_name_resp)
            init_org_body = await response_body(init_org_resp)

            print("\nPOST /api/user/changeName")
            print("status:", change_name_resp.status)
            print(pretty_text(change_name_body))

            print("\nPOST /api/organization/admin/initializeOrganization")
            print("status:", init_org_resp.status)
            print(pretty_text(init_org_body))

            if change_name_resp.status >= 400:
                raise RuntimeError("changeName 接口返回失败")
            if init_org_resp.status >= 400:
                raise RuntimeError("initializeOrganization 接口返回失败")

            print("\nfinal url:", page.url)
            print("cookies:", await context.cookies([BASE]))

            await page.wait_for_timeout(5000)
        finally:
            await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except PlaywrightTimeoutError as exc:
        print("Playwright timeout:", exc)
        raise SystemExit(1)
    except Exception as exc:
        print("ERROR:", exc)
        raise SystemExit(1)
