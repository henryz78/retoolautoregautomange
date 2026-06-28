import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from models import OrgConfig


DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"
WINDOWS_CHROME_DEVTOOLS = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "DevToolsActivePort"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a logged-in Retool org session from a local Chrome CDP session into orgs.json",
    )
    parser.add_argument(
        "--domain",
        required=True,
        help="Retool workspace domain, e.g. example.retool.com or https://example.retool.com/",
    )
    parser.add_argument(
        "--org-id",
        default="",
        help="Optional logical org id to write. Defaults to the domain name.",
    )
    parser.add_argument(
        "--gateway-config",
        default="gateway_config.json",
        help="Path to gateway_config.json. Used to resolve orgs_file.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional explicit output path for org credential JSON. Overrides orgs_file.",
    )
    parser.add_argument(
        "--cdp-endpoint",
        default=DEFAULT_CDP_ENDPOINT,
        help="Chrome DevTools endpoint, default http://127.0.0.1:9222",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace the target file with the exported org instead of merge-by-id/domain.",
    )
    parser.add_argument(
        "--check-model",
        action="append",
        default=[],
        help="Optional model alias id from gateway_config.json to verify against /api/agents. Repeatable.",
    )
    return parser.parse_args()


def normalize_domain(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        raise SystemExit("--domain is required")
    if "://" in value:
        value = urlsplit(value).netloc or value
    return value.rstrip("/")


def load_json_file(path: Path) -> Any:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def load_gateway_config_raw(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit(f"gateway config must be a JSON object: {path}")
    return data


def resolve_cdp_endpoint(raw_endpoint: str) -> str:
    endpoint = raw_endpoint.strip()
    if endpoint.startswith("ws://") or endpoint.startswith("wss://"):
        return endpoint

    if endpoint in {"http://127.0.0.1:9222", "http://localhost:9222"} and WINDOWS_CHROME_DEVTOOLS.exists():
        lines = WINDOWS_CHROME_DEVTOOLS.read_text(encoding="utf-8").splitlines()
        if len(lines) >= 2:
            port = lines[0].strip()
            ws_path = lines[1].strip()
            if port and ws_path:
                return f"ws://127.0.0.1:{port}{ws_path}"
    return endpoint


def resolve_output_path(args: argparse.Namespace, gateway_config_path: Path) -> Path:
    if args.output:
        return Path(args.output).resolve()

    gateway_config = load_gateway_config_raw(gateway_config_path.resolve())
    raw_orgs_file = gateway_config.get("orgs_file")
    if raw_orgs_file:
        orgs_path = Path(str(raw_orgs_file))
        if not orgs_path.is_absolute():
            orgs_path = (gateway_config_path.resolve().parent / orgs_path).resolve()
        return orgs_path

    raise SystemExit("gateway_config.json 未配置 orgs_file，请显式传 --output")


async def find_or_create_page(context: BrowserContext, workspace_url: str) -> Page:
    workspace_host = urlsplit(workspace_url).netloc
    for page in context.pages:
        try:
            current_host = urlsplit(page.url).netloc
        except Exception:
            current_host = ""
        if current_host == workspace_host:
            return page
    page = await context.new_page()
    await page.goto(workspace_url, wait_until="domcontentloaded", timeout=60000)
    return page


async def extract_cookie_value(context: BrowserContext, workspace_url: str, names: set[str]) -> str | None:
    cookies = await context.cookies([workspace_url])
    for cookie in cookies:
        cookie_name = str(cookie.get("name") or "")
        if cookie_name not in names:
            continue
        value = str(cookie.get("value") or "").strip()
        if value:
            return value
    return None


async def fetch_agents(page: Page, xsrf_token: str) -> dict[str, Any]:
    return await page.evaluate(
        """async ({ xsrfToken }) => {
            const response = await fetch('/api/agents', {
                method: 'GET',
                credentials: 'include',
                headers: {
                    accept: 'application/json',
                    'x-xsrf-token': xsrfToken,
                },
            });
            return {
                status: response.status,
                bodyText: await response.text(),
            };
        }""",
        {"xsrfToken": xsrf_token},
    )


def resolve_model_checks(gateway_config_path: Path, alias_ids: list[str]) -> list[dict[str, str]]:
    if not alias_ids:
        return []
    gateway_config = load_gateway_config_raw(gateway_config_path.resolve())
    raw_aliases = gateway_config.get("models")
    if not isinstance(raw_aliases, list):
        raise SystemExit(f"gateway config missing models array: {gateway_config_path}")
    aliases = {}
    for raw_alias in raw_aliases:
        if not isinstance(raw_alias, dict):
            continue
        alias_id = str(raw_alias.get("id") or "").strip()
        if alias_id:
            aliases[alias_id] = raw_alias
    checks: list[dict[str, str]] = []
    for alias_id in alias_ids:
        alias = aliases.get(alias_id)
        if not alias:
            raise SystemExit(f"Unknown model alias in gateway config: {alias_id}")
        checks.append(
            {
                "id": alias_id,
                "agent_name": str(alias.get("agent_name") or "").strip(),
                "model_name": str(alias.get("model_name") or "").strip(),
            }
        )
    return checks


def verify_agents_payload(raw_payload: dict[str, Any], checks: list[dict[str, str]]) -> None:
    body_text = str(raw_payload.get("bodyText") or "")
    status = int(raw_payload.get("status") or 0)
    if status >= 400:
        raise SystemExit(f"/api/agents failed: HTTP {status}: {body_text[:500]}")
    try:
        parsed = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"/api/agents returned invalid JSON: {exc}") from exc

    agents = parsed.get("agents")
    if not isinstance(agents, list):
        raise SystemExit("Retool /api/agents response missing agents array")

    for check in checks:
        matched = False
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            if check["agent_name"] and agent.get("name") != check["agent_name"]:
                continue
            if check["model_name"] and agent.get("data", {}).get("model") != check["model_name"]:
                continue
            matched = True
            break
        if not matched:
            raise SystemExit(
                f"Agent contract check failed for alias '{check['id']}': "
                f"agent_name={check['agent_name']!r}, model_name={check['model_name']!r}"
            )


def merge_org(existing_data: Any, exported_org: dict[str, Any], replace: bool) -> list[dict[str, Any]]:
    if replace:
        return [exported_org]

    existing_orgs = existing_data if isinstance(existing_data, list) else []
    by_key: dict[str, dict[str, Any]] = {}
    for org in existing_orgs:
        if not isinstance(org, dict):
            continue
        key = str(org.get("id") or org.get("domain_name") or "")
        if key:
            by_key[key] = org
    key = str(exported_org.get("id") or exported_org["domain_name"])
    by_key[key] = exported_org
    return list(by_key.values())


async def async_main(args: argparse.Namespace) -> None:
    domain_name = normalize_domain(args.domain)
    workspace_url = f"https://{domain_name}/"
    gateway_config_path = Path(args.gateway_config)
    output_path = resolve_output_path(args, gateway_config_path)
    checks = resolve_model_checks(gateway_config_path, args.check_model)
    cdp_endpoint = resolve_cdp_endpoint(args.cdp_endpoint)

    browser: Browser | None = None
    playwright_instance = None
    try:
        playwright_instance = await async_playwright().start()
        browser = await playwright_instance.chromium.connect_over_cdp(cdp_endpoint)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await find_or_create_page(context, workspace_url)
        await page.goto(workspace_url, wait_until="domcontentloaded", timeout=60000)

        xsrf_token = await extract_cookie_value(
            context,
            workspace_url,
            {"xsrfToken", "__Host-xsrfToken", "x-xsrf-token", "xsrf-token"},
        )
        access_token = await extract_cookie_value(context, workspace_url, {"accessToken"})
        if not xsrf_token:
            raise SystemExit(f"Workspace cookie jar missing xsrf token for {domain_name}")
        if not access_token:
            raise SystemExit(f"Workspace cookie jar missing accessToken for {domain_name}")

        if checks:
            verify_agents_payload(await fetch_agents(page, xsrf_token), checks)

        org_id = args.org_id.strip() or domain_name
        exported_org = OrgConfig.model_validate(
            {
                "id": org_id,
                "domain_name": domain_name,
                "x_xsrf_token": xsrf_token,
                "accessToken": access_token,
                "enabled": True,
            }
        ).model_dump(by_alias=True)

        merged = merge_org(load_json_file(output_path), exported_org, args.replace)
        write_json_file(output_path, merged)
        print(f"Exported org session for {domain_name} -> {output_path}")
        print(f"Org credential file now contains {len(merged)} org(s)")
        if checks:
            print("Verified model aliases:", ", ".join(check["id"] for check in checks))
    finally:
        if playwright_instance is not None:
            await playwright_instance.stop()


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
