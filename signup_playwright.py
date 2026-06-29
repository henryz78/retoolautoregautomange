import asyncio
import json
import os
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

try:
    import cloakbrowser
except ImportError:
    print("ERROR: CloakBrowser mode requires python package 'cloakbrowser'.")
    print("Please install it with: pip install cloakbrowser==0.4.3")
    raise SystemExit(1)


BASE = "https://login.retool.com"
SIGNUP_URL = f"{BASE}/auth/signup?source=navbarcta"
FOLLOWUP_URL_PART = "/auth/followup"
CLIENT_VERSION = "4.14.0-59bdefe (Build 351982)"

EMAIL = os.getenv("EMAIL", "your-email@example.com")
PASSWORD = os.getenv("PASSWORD", "your-password")
FIRST_NAME = os.getenv("FIRST_NAME", "coftens")
LAST_NAME = os.getenv("LAST_NAME", "")
SUBDOMAIN = os.getenv("SUBDOMAIN", "coftens")

HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"


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
    # 适配新的 Onboarding/Followup 页面表单。
    # 页面有 "What's your full name?" 和 "What's the name of your organization?"
    # 输入框可能使用了 placeholder，或者就是普通的 input[type="text"]。
    # 我们优先通过 placeholder 或者标签定位，如果不行再退化为 nth(0) 和 nth(1)。
    
    # 等待输入框加载并可见
    first_input = page.locator('input[placeholder="Grace Hopper"], input[name="fullName"], input[type="text"]').first
    await first_input.wait_for(state="visible", timeout=15000)
    
    # 填充姓名
    await first_input.fill(FIRST_NAME)
    
    # 填充子域名组织名称
    second_input = page.locator('input[placeholder="my-org"], input[name="subdomain"], input[type="text"]').nth(1)
    if await second_input.count() == 0:
        # 如果没有找到第二个，可能直接用 name="orgName" 或其他的。
        # 兜底：直接通过第二个普通的 text 框输入
        second_input = page.locator('input[type="text"]').nth(1)
    
    await second_input.fill(SUBDOMAIN)


async def main() -> None:
    profile_dir = os.path.join(os.path.dirname(__file__), "cloakbrowser_profile")
    os.makedirs(profile_dir, exist_ok=True)

    print("启动 CloakBrowser...")
    context = await cloakbrowser.launch_persistent_context_async(
        user_data_dir=profile_dir,
        headless=HEADLESS,
    )
    
    if context.pages:
        page = context.pages[0]
    else:
        page = await context.new_page()

    try:
        print(f"打开: {SIGNUP_URL}")
        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        await wait_for_signup_form(page)
        print("注册表单已出现")

        await fill_signup_form(page)

        continue_button = page.get_by_role("button", name="Continue").last
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

        # 稍微等一秒，确保新的 DOM 结构渲染完全，因为可能存在前端渲染延迟
        await page.wait_for_timeout(2000)
        await fill_followup_form(page)

        # 稍微等待子域名冲突检测的 "Checking..." 消失（在 Continue 上方）
        # 页面上有 "Checking..." 动画或按钮变灰色
        # 我们这里等待一下，最长等 5 秒，或者直接点击
        await page.wait_for_timeout(2000)

        followup_button = page.get_by_role("button", name="Continue").last
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
        try:
            await page.screenshot(path="screenshot.png")
            print("保存最终截图至 screenshot.png")
        except Exception:
            pass
        await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except PlaywrightTimeoutError as exc:
        print("Playwright timeout:", exc)
        raise SystemExit(1)
    except Exception as exc:
        print("ERROR:", exc)
        raise SystemExit(1)
