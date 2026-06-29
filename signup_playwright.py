import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
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


# ================= Retool Workspace Client & Helpers =================

@dataclass
class AgentConfig:
    name: str
    description: str
    model: str
    provider: str
    temperature: float
    max_iterations: int
    instructions: str
    thinking_enabled: bool


class RetoolWorkspaceClient:
    def __init__(self, page, workspace_base_url: str):
        self.page = page
        self.workspace_base_url = workspace_base_url.rstrip("/")

    async def get_xsrf_token(self) -> str:
        cookies = await self.page.context.cookies([self.workspace_base_url])
        for cookie in cookies:
            name = str(cookie.get("name") or "")
            if name.lower() in {"x-xsrf-token", "xsrf-token", "xsrftoken", "__host-xsrftoken"}:
                value = str(cookie.get("value") or "").strip()
                if value:
                    return value
        raise RuntimeError("当前 workspace 登录态中未找到 XSRF cookie")

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        response = await self.page.context.request.fetch(
            f"{self.workspace_base_url}{path}",
            method=method,
            params=params,
            data=payload,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": self.workspace_base_url,
                "referer": f"{self.workspace_base_url}/",
                "x-retool-client-version": CLIENT_VERSION,
                "x-xsrf-token": await self.get_xsrf_token(),
            },
        )
        body_text = await response.text()
        try:
            body = json.loads(body_text)
        except Exception:
            body = body_text
        if response.status >= 400:
            raise RuntimeError(
                f"Retool API 失败: {method} {path} -> HTTP {response.status}: {body}"
            )
        return body

    async def get_ai_settings(self) -> Any:
        return await self.request("GET", "/api/aiSettings")

    async def get_agents_metadata(self) -> Any:
        return await self.request("GET", "/api/agents/agentsMetadata")

    async def get_environments(self) -> Any:
        return await self.request("GET", "/api/environments")

    async def get_workflow(self, workflow_id: str) -> Any:
        return await self.request("GET", f"/api/workflow/{workflow_id}")

    async def create_workflow(self, payload: dict[str, Any]) -> Any:
        return await self.request("POST", "/api/workflow", payload=payload)

    async def save_workflow(self, workflow_id: str, new_workflow_data: dict[str, Any]) -> Any:
        return await self.request("POST", f"/api/workflow/{workflow_id}", payload={"newWorkflowData": new_workflow_data})

    async def release_workflow(
        self,
        workflow_id: str,
        workflow_save_id: str,
        *,
        name: str = "0.0.2",
        description: str = "",
        additional_workflows: list[Any] | None = None,
    ) -> Any:
        return await self.request(
            "POST",
            "/api/workflowRelease",
            payload={
                "workflowId": workflow_id,
                "workflowSaveId": workflow_save_id,
                "name": name,
                "description": description,
                "additionalWorkflows": additional_workflows or [],
            },
        )


def _replace_template_field(pattern: str, replacement: str, template_data: str, field_name: str) -> str:
    updated, count = re.subn(pattern, replacement, template_data, count=1)
    if count != 1:
        raise RuntimeError(f"未能在 agent templateData 中定位字段: {field_name}")
    return updated


def replace_template_string_field(template_data: str, field_name: str, value: str) -> str:
    pattern = rf'("{re.escape(field_name)}",)"(?:\\.|[^"\\])*"'
    replacement = lambda match: f"{match.group(1)}{json.dumps(value, ensure_ascii=False)}"
    updated, count = re.subn(pattern, replacement, template_data, count=1)
    if count != 1:
        raise RuntimeError(f"未能在 agent templateData 中定位字符串字段: {field_name}")
    return updated


def upsert_template_string_field(
    template_data: str,
    field_name: str,
    value: str,
    *,
    insert_after_field: str | None = None,
) -> str:
    pattern = rf'("{re.escape(field_name)}",)"(?:\\.|[^"\\])*"'
    if re.search(pattern, template_data):
        return replace_template_string_field(template_data, field_name, value)

    if not insert_after_field:
        raise RuntimeError(f"未能在 agent templateData 中插入字符串字段: {field_name}")

    insert_pattern = rf'("{re.escape(insert_after_field)}",)"(?:\\.|[^"\\])*"'
    insert_fragment = f',{json.dumps(field_name, ensure_ascii=False)},{json.dumps(value, ensure_ascii=False)}'

    def repl(match: re.Match[str]) -> str:
        return f"{match.group(0)}{insert_fragment}"

    updated, count = re.subn(insert_pattern, repl, template_data, count=1)
    if count != 1:
        raise RuntimeError(
            f"未能在 agent templateData 中新增字段 {field_name}，缺少锚点字段: {insert_after_field}"
        )
    return updated


def replace_template_numeric_field(template_data: str, field_name: str, value: int | float) -> str:
    pattern = rf'("{re.escape(field_name)}",)-?\d+(?:\.\d+)?'
    return _replace_template_field(pattern, rf'\g<1>{value}', template_data, field_name)


def replace_template_bool_field(template_data: str, field_name: str, value: bool) -> str:
    pattern = rf'("{re.escape(field_name)}",)(true|false)'
    bool_literal = "true" if value else "false"
    return _replace_template_field(pattern, rf"\g<1>{bool_literal}", template_data, field_name)


def build_agent_template_data(
    template_data: str,
    *,
    provider_id: str,
    provider_name: str,
    provider_resource_name: str,
    instructions: str,
    model: str,
    temperature: float,
    max_iterations: int,
    thinking_enabled: bool,
) -> str:
    updated = template_data
    updated = replace_template_string_field(updated, "providerId", provider_id)
    updated = replace_template_string_field(updated, "providerName", provider_name)
    updated = replace_template_string_field(updated, "model", model)
    updated = upsert_template_string_field(
        updated,
        "providerResourceName",
        provider_resource_name,
        insert_after_field="model",
    )
    updated = replace_template_string_field(updated, "instructions", instructions)
    updated = replace_template_numeric_field(updated, "temperature", temperature)
    updated = replace_template_numeric_field(updated, "maxIterations", max_iterations)
    updated = replace_template_bool_field(updated, "thinkingEnabled", thinking_enabled)
    return updated


def resolve_agent_provider(ai_settings: Any, provider: str) -> tuple[str, str, str]:
    if not isinstance(ai_settings, dict):
        raise RuntimeError("aiSettings 响应格式异常")

    if provider == "openai":
        resource_name = ai_settings.get("assistOpenAIResourceName")
        provider_id = "retoolAIBuiltIn::openAI"
        provider_name = "openAI"
    elif provider == "anthropic":
        resource_name = ai_settings.get("assistAnthropicResourceName")
        provider_id = "retoolAIBuiltIn::anthropic"
        provider_name = "anthropic"
    else:
        raise RuntimeError(f"不支持的 agent provider: {provider}")

    if not isinstance(resource_name, str) or not resource_name.strip():
        raise RuntimeError(f"aiSettings 中未返回 provider 资源名: {provider}")

    return provider_id, provider_name, resource_name.strip()


async def create_and_configure_agent(
    page,
    workspace_base_url: str,
    agent_config: AgentConfig,
) -> dict[str, Any]:
    print(f"  -> 正在网页点击创建机器人 {agent_config.name} ({agent_config.model})...")
    
    # 1. 导航到 AI 页面，增加重试以防刚注册完页面还没初始化好
    target_url = f"{workspace_base_url}/agents"
    create_btn = page.locator('button:has-text("Agent"), button:has-text("Create agent"), [data-testid="EmptyState::CreateAgent"]').first
    
    for load_attempt in range(1, 4):
        try:
            print(f"  -> 导航至 /agents (第 {load_attempt}/3 次尝试)...")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(3000)
            await create_btn.wait_for(state="visible", timeout=12000)
            break
        except PlaywrightTimeoutError as exc:
            if load_attempt == 3:
                raise RuntimeError(f"加载 /agents 页面超时，未能找到创建按钮: {exc}")
            print(f"  -> 页面加载缓慢或按钮未出现，重试页面刷新...")
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            
    await create_btn.click()
    await page.wait_for_timeout(2000)
    
    # 2. 点击 "Start from scratch" 卡片
    start_scratch = page.locator('div[class*="modal"] :text("Start from scratch"), div[class*="modal"] :text-matches("Start from scratch", "i")').first
    await start_scratch.wait_for(state="visible", timeout=15000)
    await start_scratch.click()
    await page.wait_for_timeout(1000)
    
    # 3. 填入机器人名字 (如 gpt 或 claude) —— 注意：必须先填名字，底部的 Create 按钮才会从禁用变成启用状态！
    name_input = page.locator('div[class*="modal"] input[placeholder="Weather Agent"], div[class*="modal"] input[placeholder="Agent name"], div[class*="modal"] input[type="text"]').first
    await name_input.wait_for(state="visible", timeout=15000)
    await name_input.fill(agent_config.name)
    await page.wait_for_timeout(1000)
    
    # 5. 点击确认创建按钮 (真正开始创建并跳转)
    confirm_btn = page.locator('div[class*="modal"] button:has-text("Create"), div[class*="modal"] button[type="submit"]').first
    await confirm_btn.click()
    
    # 6. 等待页面跳转到编辑器
    workflow_id = ""
    for _ in range(25):
        await page.wait_for_timeout(1000)
        print(f"  -> 等待跳转中... 当前 URL: {page.url}")
        # 兼容匹配所有可能路径下的 36 位 UUID (不管它是 /rr/edit/ 还是 /agents/ 还是 /workflows/)
        match = re.search(r"/([a-f0-9\-]{36})", page.url)
        if match:
            workflow_id = match.group(1)
            break
            
    if not workflow_id:
        raise RuntimeError("网页创建机器人后未能跳转到编辑器页面获取 workflowId")

    print(f"  -> 成功获取新创建的机器人的 workflowId: {workflow_id}")
    
    # 7. 使用 API 进行高智商配置
    workspace_client = RetoolWorkspaceClient(page, workspace_base_url)
    ai_settings = await workspace_client.get_ai_settings()
    
    provider_id, provider_name, provider_resource_name = resolve_agent_provider(ai_settings, agent_config.provider)
    
    # 获取刚刚创建的默认 workflow 配置
    created_workflow_response = await workspace_client.get_workflow(workflow_id)
    if isinstance(created_workflow_response, dict) and "workflow" in created_workflow_response:
        created_workflow = created_workflow_response["workflow"]
    elif isinstance(created_workflow_response, dict) and "data" in created_workflow_response:
        created_workflow = created_workflow_response["data"]
    else:
        created_workflow = created_workflow_response

    template_data = created_workflow.get("templateData")
    if not isinstance(template_data, str) or not template_data:
        raise RuntimeError("未能获取新创建机器人的 templateData")

    updated_template_data = build_agent_template_data(
        template_data,
        provider_id=provider_id,
        provider_name=provider_name,
        provider_resource_name=provider_resource_name,
        instructions=agent_config.instructions,
        model=agent_config.model,
        temperature=agent_config.temperature,
        max_iterations=agent_config.max_iterations,
        thinking_enabled=agent_config.thinking_enabled,
    )

    workflow_data = json.loads(json.dumps(created_workflow))
    workflow_data["name"] = agent_config.name
    workflow_data["description"] = agent_config.description
    workflow_data["templateData"] = updated_template_data

    saved_workflow = await workspace_client.save_workflow(workflow_id, workflow_data)
    workflow_save_id = saved_workflow.get("saveId")
    if not isinstance(workflow_save_id, str) or not workflow_save_id.strip():
        raise RuntimeError("保存 agent 配置后未返回 saveId")

    await workspace_client.release_workflow(
        workflow_id,
        workflow_save_id.strip(),
    )
    
    # 8. 回到工作空间主页
    await page.goto(workspace_base_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)
    print(f"  -> 机器人 {agent_config.name} 配置发布完成！")
    return {
        "workflowId": workflow_id,
        "name": agent_config.name,
    }


async def create_and_configure_agents(page, workspace_base_url: str) -> None:
    agent_configs = [
        AgentConfig(
            name=os.getenv("RET0OL_AGENT_NAME", "gpt"),
            description="",
            model=os.getenv("RET0OL_AGENT_MODEL", "gpt-5.5"), 
            provider="openai",
            temperature=0.3,
            max_iterations=50,
            instructions="",
            thinking_enabled=False,
        ),
        AgentConfig(
            name=os.getenv("RET0OL_AGENT_CLAUDE_NAME", "claude"),
            description="",
            model=os.getenv("RET0OL_AGENT_CLAUDE_MODEL", "claude-3-5-sonnet"), 
            provider="anthropic",
            temperature=0.3,
            max_iterations=10,
            instructions="",
            thinking_enabled=False,
        )
    ]

    for config in agent_configs:
        await create_and_configure_agent(page, workspace_base_url, config)


# =====================================================================


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
    first_input = page.locator('input[placeholder="Grace Hopper"], input[name="fullName"], input[type="text"]').first
    await first_input.wait_for(state="visible", timeout=15000)
    await first_input.fill(FIRST_NAME)
    
    second_input = page.locator('input[placeholder="my-org"], input[name="subdomain"], input[type="text"]').nth(1)
    if await second_input.count() == 0:
        second_input = page.locator('input[type="text"]').nth(1)
    
    await second_input.fill(SUBDOMAIN)


async def run_signup_attempt() -> bool:
    profile_dir = os.path.join(os.path.dirname(__file__), "cloakbrowser_profile")
    os.makedirs(profile_dir, exist_ok=True)

    print("启动 CloakBrowser...")
    try:
        context = await cloakbrowser.launch_persistent_context_async(
            user_data_dir=profile_dir,
            headless=HEADLESS,
        )
    except Exception as exc:
        print(f"启动浏览器失败: {exc}")
        return False
    
    if context.pages:
        page = context.pages[0]
    else:
        page = await context.new_page()

    try:
        print(f"打开: {SIGNUP_URL}")
        # 设置超时时间为 45 秒，避免无限等待
        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
        
        # 等待注册表单，超时缩短到 35 秒以便快速失败并重试
        email_locator = page.locator('input[name="email"], input[placeholder="Work email"]')
        form_appeared = False
        for _ in range(18):
            if await email_locator.count() > 0 and await email_locator.first.is_visible():
                form_appeared = True
                break
            title = await page.title()
            if "Just a moment" in title:
                print("等待 Cloudflare 挑战通过...")
            await page.wait_for_timeout(2000)
            
        if not form_appeared:
            print("[WARN] 35 秒内未出现注册表单，可能卡在 Cloudflare。")
            return False

        print("注册表单已出现")
        await fill_signup_form(page)

        continue_button = page.get_by_role("button", name="Continue").last
        if await continue_button.count() == 0:
            print("[WARN] 未找到注册页 Continue 按钮")
            return False

        async with page.expect_response(
            lambda resp: "/api/signup" in resp.url and resp.request.method == "POST",
            timeout=45000,
        ) as signup_info:
            await continue_button.click()

        signup_resp = await signup_info.value
        signup_body = await response_body(signup_resp)
        print("\nPOST /api/signup")
        print("status:", signup_resp.status)
        print(pretty_text(signup_body))

        if signup_resp.status >= 400:
            print("[WARN] signup 接口返回失败")
            return False

        print("等待页面重定向...")
        try:
            await page.wait_for_url(
                lambda url: "/auth/followup" in url or "/auth/verifyEmail" in url,
                timeout=25000
            )
        except PlaywrightTimeoutError:
            try:
                res_data = json.loads(signup_body)
                redirect_uri = res_data.get("redirectUri")
                if redirect_uri:
                    print(f"自动重定向超时，手动跳转至: {BASE}{redirect_uri}")
                    await page.goto(f"{BASE}{redirect_uri}", wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

        print("\n当前页面 URL:", page.url)

        if "/auth/verifyEmail" in page.url:
            print("检测到同域名团队提示，选择创建新组织...")
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)
            
            create_org_btn = page.get_by_text("create a new organization", exact=False).first
            await create_org_btn.wait_for(state="visible", timeout=15000)
            await create_org_btn.click()
            await page.wait_for_timeout(2000)
            
            ok_btn = page.get_by_role("button", name=re.compile("^ok$", re.IGNORECASE)).first
            if await ok_btn.count() > 0:
                print("点击 OK 二次确认...")
                await ok_btn.click()
                await page.wait_for_timeout(2000)

        await page.wait_for_url(f"**{FOLLOWUP_URL_PART}**", timeout=30000)
        print("已成功到达 followup 页面:", page.url)

        await page.wait_for_timeout(2000)
        await fill_followup_form(page)
        await page.wait_for_timeout(2000)

        followup_button = page.get_by_role("button", name="Continue").last
        if await followup_button.count() == 0:
            print("[WARN] 未找到 followup Continue 按钮")
            return False

        async with page.expect_response(
            lambda resp: "/api/user/changeName" in resp.url and resp.request.method == "POST",
            timeout=45000,
        ) as change_name_info:
            async with page.expect_response(
                lambda resp: "/api/organization/admin/initializeOrganization" in resp.url and resp.request.method == "POST",
                timeout=45000,
            ) as init_org_info:
                await followup_button.click()

        change_name_resp = await change_name_info.value
        init_org_resp = await init_org_info.value

        change_name_body = await response_body(change_name_resp)
        init_org_body = await response_body(init_org_resp)

        print("\nPOST /api/user/changeName")
        print("status:", change_name_resp.status)
        print(pretty_text(change_name_body))

        print("\nPOST /api/organization/admin/initializeOrganization")
        print("status:", init_org_resp.status)
        print(pretty_text(init_org_body))

        if change_name_resp.status >= 400 or init_org_resp.status >= 400:
            print("[WARN] 初始化组织或修改名字失败")
            return False

        print("\n处理后续问卷调查...")
        for i in range(5):
            await page.wait_for_load_state("domcontentloaded")
            url = page.url
            print(f"当前页面 URL: {url}")
            
            if "/auth/role" in url:
                print("正在选择角色: Software Engineering...")
                role_locator = page.get_by_text("Software Engineering")
                await role_locator.first.click()
                await page.get_by_role("button", name="Continue").last.click()
                await page.wait_for_timeout(3000)
            elif "/auth/familiarity" in url:
                print("正在选择熟悉度: Advanced...")
                fam_locator = page.get_by_text("Advanced")
                await fam_locator.first.click()
                await page.get_by_role("button", name="Continue").last.click()
                await page.wait_for_timeout(3000)
            elif "/auth/referralForm" in url:
                print("正在选择推荐来源: Web search...")
                ref_locator = page.get_by_text("Web search")
                await ref_locator.first.click()
                await page.get_by_role("button", name="Continue").last.click()
                await page.wait_for_timeout(5000)
            elif "/resources" in url or "/apps" in url or ("retool.com" in url and "auth" not in url):
                print("已成功到达控制台，问卷填写完毕！")
                break
            else:
                await page.wait_for_timeout(2000)

        print("\nfinal url:", page.url)

        # ----------------- 自动配置 AI 机器人 -----------------
        # 在跳转到 /agents 之前，先让新注册的工作空间彻底“冷静/就绪” 6 秒钟，防止接口报错
        print("等待 6 秒使工作空间就绪...")
        await page.wait_for_timeout(6000)
        
        workspace_base_url = f"https://{SUBDOMAIN}.retool.com"
        print("等待工作空间接口就绪...")
        for _ in range(30):
            if page.url.startswith(workspace_base_url) and "auth" not in page.url:
                break
            await page.wait_for_timeout(1000)

        print(f"正在自动创建并配置 AI 机器人 (gpt-5.5 & Claude 3.5 Sonnet)...")
        await create_and_configure_agents(page, workspace_base_url)
        print("AI 机器人全自动配置完成！")
        # -----------------------------------------------------

        cookies = await context.cookies([BASE, f"https://{SUBDOMAIN}.retool.com"])
        xsrf_token = ""
        access_token = ""
        for cookie in cookies:
            if cookie["name"] == "xsrfToken":
                xsrf_token = cookie["value"]
            elif cookie["name"] == "accessToken":
                access_token = cookie["value"]

        if xsrf_token and access_token:
            now_time = int(time.time())
            expires_at = now_time + 7 * 24 * 60 * 60
            
            session_org = {
                "id": SUBDOMAIN,
                "domain_name": f"{SUBDOMAIN}.retool.com",
                "x_xsrf_token": xsrf_token,
                "accessToken": access_token,
                "enabled": True,
                "source_email": EMAIL,
                "refreshed_at": now_time,
                "expires_at": expires_at,
                "verified_models": ["gpt-5.5", "claude-sonnet-4-6"]
            }
            
            bundle_dir = os.path.join(os.path.dirname(__file__), "manage", "runtime")
            os.makedirs(bundle_dir, exist_ok=True)
            bundle_path = os.path.join(bundle_dir, "session_bundle.json")
            
            orgs_list = []
            if os.path.exists(bundle_path):
                try:
                    with open(bundle_path, "r", encoding="utf-8") as f:
                        old_bundle = json.load(f)
                        if isinstance(old_bundle, dict) and "orgs" in old_bundle:
                            orgs_list = old_bundle["orgs"]
                except Exception:
                    pass
            
            orgs_list = [o for o in orgs_list if o.get("id") != SUBDOMAIN and o.get("domain_name") != f"{SUBDOMAIN}.retool.com"]
            orgs_list.append(session_org)
            
            bundle_data = {
                "bundle_version": "1",
                "generated_at": now_time,
                "generated_by": {
                    "tool": "retoolautoregautomange",
                    "script": "signup_playwright.py"
                },
                "expires_at": expires_at,
                "org_count": len(orgs_list),
                "verified_models": ["gpt-5.5", "claude-sonnet-4-6"],
                "orgs": orgs_list
            }
            
            with open(bundle_path, "w", encoding="utf-8") as f:
                json.dump(bundle_data, f, indent=2, ensure_ascii=False)
            
            print(f"\n[OK] 成功自动将本次注册的登录态保存至符合网关规范的: manage/runtime/session_bundle.json !")
            return True
        else:
            print("[WARN] 未能在 Cookie 中提取到登录凭证")
            return False

    except Exception as exc:
        print(f"[WARN] 本次尝试捕获到异常: {exc}")
        return False
    finally:
        try:
            await page.screenshot(path="screenshot.png")
            print("保存最终截图至 screenshot.png")
        except Exception:
            pass
        try:
            await context.close()
        except Exception:
            pass


async def main() -> None:
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        print(f"\n[尝试 {attempt}/{max_attempts}] 启动全自动注册流程...")
        success = await run_signup_attempt()
        if success:
            print("[OK] 全自动注册、机器人配置、数据保存全部圆满成功！")
            return
        else:
            print(f"[WARN] 第 {attempt} 次尝试未能完全跑通，正在清理环境并在 5 秒后自动重试...")
            await asyncio.sleep(5)
            
    print("\n[ERROR] 连续 3 次尝试均告失败，程序退出。")
    raise SystemExit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except PlaywrightTimeoutError as exc:
        print("Playwright timeout:", exc)
        raise SystemExit(1)
    except Exception as exc:
        print("ERROR:", exc)
        raise SystemExit(1)
