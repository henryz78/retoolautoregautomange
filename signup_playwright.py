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


def load_agent_create_seed_payload() -> dict[str, Any]:
    from urllib.parse import urlsplit
    har_path = os.path.join(os.path.dirname(__file__), "3、创建agent配置agent模型.har")
    try:
        with open(har_path, "r", encoding="utf-8") as har_file:
            har_payload = json.load(har_file)
    except OSError as exc:
        raise RuntimeError(f"无法读取 agent 创建 HAR 文件: {har_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"agent 创建 HAR 不是有效 JSON: {har_path}") from exc

    entries = har_payload.get("log", {}).get("entries", [])
    for entry in entries:
        request = entry.get("request", {})
        if request.get("method") != "POST":
            continue
        if urlsplit(str(request.get("url") or "")).path != "/api/workflow":
            continue

        post_text = request.get("postData", {}).get("text")
        if not isinstance(post_text, str) or not post_text.strip():
            raise RuntimeError("agent 创建 HAR 中的 /api/workflow 请求缺少 postData.text")

        try:
            payload = json.loads(post_text)
        except Exception:
            payload = post_text
        if not isinstance(payload, dict):
            raise RuntimeError("agent 创建 HAR 中的 /api/workflow 请求体格式异常")
        return payload

    raise RuntimeError("agent 创建 HAR 中未找到 POST /api/workflow 请求")


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


def resolve_agent_root_folder_id(agents_metadata: Any) -> int:
    if not isinstance(agents_metadata, dict):
        raise RuntimeError("agentsMetadata 响应格式异常")

    folders = agents_metadata.get("agentFolders")
    if not isinstance(folders, list) or not folders:
        raise RuntimeError("agentsMetadata 中缺少 agentFolders")

    for folder in folders:
        if isinstance(folder, dict) and folder.get("systemFolder") and folder.get("folderType") == "agent":
            folder_id = folder.get("id")
            if isinstance(folder_id, int):
                return folder_id

    first_folder = folders[0]
    if isinstance(first_folder, dict) and isinstance(first_folder.get("id"), int):
        return first_folder["id"]

    raise RuntimeError("未能从 agentsMetadata 中解析 agent folderId")


def collect_existing_agent_names(agents_metadata: Any) -> set[str]:
    if not isinstance(agents_metadata, dict):
        return set()
    agents = agents_metadata.get("agentsMetadata")
    if not isinstance(agents, list):
        return set()
    names: set[str] = set()
    for agent in agents:
        if isinstance(agent, dict):
            name = agent.get("name")
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    return names


def build_unique_agent_name(preferred_name: str, existing_names: set[str]) -> str:
    if preferred_name not in existing_names:
        return preferred_name

    suffix = 2
    while True:
        candidate = f"{preferred_name}-{suffix}"
        if candidate not in existing_names:
            return candidate
        suffix += 1


async def create_and_configure_agent(
    page,
    workspace_base_url: str,
    agent_config: AgentConfig,
) -> dict[str, Any]:
    workspace_client = RetoolWorkspaceClient(page, workspace_base_url)
    ai_settings = await workspace_client.get_ai_settings()
    agents_metadata = await workspace_client.get_agents_metadata()
    environments = await workspace_client.get_environments()

    environments_list = environments.get("environments") if isinstance(environments, dict) else None
    if not isinstance(environments_list, list) or not environments_list:
        raise RuntimeError("workspace environments 未初始化完成，暂时不能创建 agent")

    provider_id, provider_name, provider_resource_name = resolve_agent_provider(ai_settings, agent_config.provider)
    folder_id = resolve_agent_root_folder_id(agents_metadata)
    existing_names = collect_existing_agent_names(agents_metadata)
    preferred_name = agent_config.name.strip() or "agent"
    agent_name = build_unique_agent_name(preferred_name, existing_names)

    seed_payload = load_agent_create_seed_payload()
    create_payload = json.loads(json.dumps(seed_payload))
    create_payload["name"] = agent_name
    create_payload["description"] = agent_config.description
    create_payload["folderId"] = folder_id

    print(f"  -> 正在通过 API 创建 AI 机器人: {agent_name} ({agent_config.model})...")
    created_workflow = await workspace_client.create_workflow(create_payload)
    if not isinstance(created_workflow, dict):
        raise RuntimeError("创建 agent 后返回数据格式异常")

    workflow_id = created_workflow.get("id")
    template_data = created_workflow.get("templateData")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise RuntimeError("创建 agent 后未返回 workflowId")
    if not isinstance(template_data, str) or not template_data:
        raise RuntimeError("创建 agent 后未返回 templateData")

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
    workflow_data["name"] = agent_name
    workflow_data["description"] = agent_config.description
    workflow_data["folderId"] = folder_id
    workflow_data["templateData"] = updated_template_data

    saved_workflow = await workspace_client.save_workflow(workflow_id, workflow_data)
    if not isinstance(saved_workflow, dict):
        raise RuntimeError("保存 agent 配置后返回数据格式异常")

    workflow_save_id = saved_workflow.get("saveId")
    if not isinstance(workflow_save_id, str) or not workflow_save_id.strip():
        raise RuntimeError("保存 agent 配置后未返回 workflow saveId")

    release_info = await workspace_client.release_workflow(
        workflow_id,
        workflow_save_id.strip(),
    )
    if not isinstance(release_info, dict):
        raise RuntimeError("发布 agent 后返回数据格式异常")

    print(f"  -> 机器人 {agent_name} 创建发布成功！")
    return {
        "workflowId": workflow_id,
        "name": agent_name,
    }


async def create_agent_via_ui_and_configure(
    page,
    workspace_base_url: str,
    agent_config: AgentConfig,
) -> dict[str, Any]:
    print(f"  -> [UI-API 混合模式] 正在网页点击创建机器人 {agent_config.name}...")
    
    # 1. 导航到 AI 页面
    await page.goto(f"{workspace_base_url}/agents", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)
    
    # 2. 点击 "Create agent" 或 "+ Agent" 按钮
    create_btn = page.locator('button:has-text("Agent"), button:has-text("Create agent"), button:has-text("Create")').first
    await create_btn.wait_for(state="visible", timeout=15000)
    await create_btn.click()
    await page.wait_for_timeout(2000)
    
    # 3. 点击 "Start from scratch" 卡片
    start_scratch = page.get_by_text("Start from scratch").first
    await start_scratch.wait_for(state="visible", timeout=15000)
    await start_scratch.click()
    await page.wait_for_timeout(1000)
    
    # 4. 点击右下角的 Create 按钮 (进入填名字界面)
    next_btn = page.locator('button:has-text("Create")').last
    await next_btn.click()
    await page.wait_for_timeout(1500)

    # 5. 填入机器人名字 (如 gpt 或 claude)
    name_input = page.locator('input[placeholder="Weather Agent"], input[placeholder="Agent name"], input[type="text"], input[name="name"]').first
    await name_input.wait_for(state="visible", timeout=10000)
    await name_input.fill(agent_config.name)
    await page.wait_for_timeout(500)
    
    # 6. 点击确认创建按钮 (真正开始创建并跳转)
    confirm_btn = page.locator('button:has-text("Create"), button:has-text("Save"), button[type="submit"]').last
    await confirm_btn.click()
    
    # 7. 等待页面跳转到编辑器
    workflow_id = ""
    for _ in range(20):
        await page.wait_for_timeout(1000)
        match = re.search(r"/rr/edit/([a-f0-9\-]{36})", page.url)
        if match:
            workflow_id = match.group(1)
            break
            
    if not workflow_id:
        raise RuntimeError("网页创建机器人后未能跳转到编辑器页面获取 workflowId")

    print(f"  -> [UI-API 混合模式] 成功获取新创建的机器人的 workflowId: {workflow_id}")
    
    # 8. 使用 API 进行高智商配置
    workspace_client = RetoolWorkspaceClient(page, workspace_base_url)
    ai_settings = await workspace_client.get_ai_settings()
    
    provider_id, provider_name, provider_resource_name = resolve_agent_provider(ai_settings, agent_config.provider)
    
    # 获取刚刚创建的默认 workflow 配置
    created_workflow = await workspace_client.get_workflow(workflow_id)
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
    
    # 9. 回到工作空间主页
    await page.goto(workspace_base_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)
    print(f"  -> [UI-API 混合模式] 机器人 {agent_config.name} 配置发布完成！")
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
        try:
            # 优先用 API 模板创建（高智商配置）
            await create_and_configure_agent(page, workspace_base_url, config)
        except Exception as exc:
            # 如果 HAR 文件丢失，启动 UI 自动点击创建机器人，然后 API 重命名并发布！
            print(f"[WARN] 模板创建失败 ({exc})。启动 UI 混合模式创建配置...")
            try:
                await create_agent_via_ui_and_configure(page, workspace_base_url, config)
            except Exception as ui_exc:
                print(f"[ERROR] 网页 UI 自动创建配置机器人 {config.name} 也失败了: {ui_exc}")


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
            name_input = page.locator('input[placeholder="Grace Hopper"], input[type="text"]').first
            await name_input.wait_for(state="visible", timeout=15000)
            await name_input.fill(FIRST_NAME)
            
            create_org_btn = page.get_by_text("create a new organization", exact=False).first
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
            raise RuntimeError("未找到 followup Continue 按钮")

        async with page.expect_response(
            lambda resp: "/api/user/changeName" in resp.url and resp.request.method == "POST",
            timeout=60000,
        ) as change_name_info:
            async with page.expect_response(
                lambda resp: "/api/organization/admin/initializeOrganization" in resp.url and resp.request.method == "POST",
                timeout=60000,
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

        if change_name_resp.status >= 400:
            raise RuntimeError("changeName 接口返回失败")
        if init_org_resp.status >= 400:
            raise RuntimeError("initializeOrganization 接口返回失败")

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
        workspace_base_url = f"https://{SUBDOMAIN}.retool.com"
        print("等待工作空间接口就绪...")
        for _ in range(30):
            if page.url.startswith(workspace_base_url) and "auth" not in page.url:
                break
            await page.wait_for_timeout(1000)

        print("正在自动创建并配置 AI 机器人 (gpt-5.5 & Claude 3.5 Sonnet)...")
        try:
            await create_and_configure_agents(page, workspace_base_url)
            print("AI 机器人全自动配置完成！")
        except Exception as exc:
            print(f"[WARN] 自动配置 AI 机器人失败: {exc}，请稍后手动在网页创建。")
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
