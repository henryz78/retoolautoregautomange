import json
import os
import re
import urllib.request
import urllib.error
from pathlib import Path

def request_retool(method: str, url: str, access_token: str, xsrf_token: str, payload: dict = None) -> dict:
    req = urllib.request.Request(url, method=method)
    req.add_header("accept", "application/json")
    req.add_header("content-type", "application/json")
    req.add_header("cookie", f"accessToken={access_token}; xsrfToken={xsrf_token}")
    req.add_header("x-xsrf-token", xsrf_token)
    req.add_header("user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    data = None
    if payload:
        data = json.dumps(payload).encode("utf-8")
        
    try:
        with urllib.request.urlopen(req, data=data) as response:
            res_body = response.read().decode("utf-8")
            return json.loads(res_body)
    except urllib.error.HTTPError as e:
        print(f"请求失败: {method} {url} -> HTTP Error {e.code}: {e.reason}")
        try:
            print("错误详情:", e.read().decode("utf-8")[:1000])
        except Exception:
            pass
        return {}
    except Exception as e:
        print(f"请求失败: {method} {url} -> {e}")
        return {}

def main():
    bundle_path = Path("runtime/session_bundle.json")
    if not bundle_path.exists():
        bundle_path = Path("manage/runtime/session_bundle.json")
    if not bundle_path.exists():
        bundle_path = Path("../manage/runtime/session_bundle.json")
        
    if not bundle_path.exists():
        print("未找到 session_bundle.json 文件！")
        return
        
    with open(bundle_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)
        
    if not bundle.get("orgs"):
        print("session_bundle.json 中没有找到组织数据！")
        return
        
    org = bundle["orgs"][-1]  # 获取最新一个组织
    subdomain = org["id"]
    access_token = org["accessToken"]
    xsrf_token = org["x_xsrf_token"]
    
    workspace_url = f"https://{subdomain}.retool.com"
    print(f"正在诊断工作空间: {workspace_url}")
    print(f"绑定的注册邮箱: {org.get('source_email')}")
    print("-" * 50)
    
    # 1. 获取 aiSettings
    ai_settings = request_retool("GET", f"{workspace_url}/api/aiSettings", access_token, xsrf_token)
    print("AI Settings 资源诊断:")
    print(f"  - assistOpenAIResourceName: {ai_settings.get('assistOpenAIResourceName')}")
    print(f"  - assistAnthropicResourceName: {ai_settings.get('assistAnthropicResourceName')}")
    print("-" * 50)
    
    # 2. 获取所有的 agents
    agents_data = request_retool("GET", f"{workspace_url}/api/agents", access_token, xsrf_token)
    agents = agents_data.get("agents", [])
    print(f"当前账号中已创建的机器人数量: {len(agents)}")
    
    for agent in agents:
        agent_name = agent.get("name")
        workflow_id = agent.get("id")
        print(f"\n机器人名: [{agent_name}] (UUID: {workflow_id})")
        
        # 获取该机器人的详细 workflow 配置
        wf_res = request_retool("GET", f"{workspace_url}/api/workflow/{workflow_id}", access_token, xsrf_token)
        wf = wf_res.get("workflow", wf_res.get("data", wf_res))
        
        template_data = wf.get("templateData", "")
        if not template_data:
            print("  [WARN] 机器人的 templateData 为空！")
            continue
            
        print("  当前实际配置参数:")
        
        # 使用正则提取展示
        def extract(field):
            m = re.search(rf'"{re.escape(field)}","((?:\\.|[^"\\])*)"', template_data)
            return json.loads(f'"{m.group(1)}"') if m else "未找到"
            
        print(f"    - providerId: {extract('providerId')}")
        print(f"    - providerName: {extract('providerName')}")
        print(f"    - model: {extract('model')}")
        print(f"    - instructions: {repr(extract('instructions'))}")
        
    print("-" * 50)
    
    # 3. 诊断当前网关 orgs.json 里的活动账号
    orgs_json_path = Path("orgs.json")
    if not orgs_json_path.exists():
        orgs_json_path = Path("manage/orgs.json")
    if not orgs_json_path.exists():
        orgs_json_path = Path("../manage/orgs.json")
        
    print("网关 orgs.json 活动路由诊断:")
    if orgs_json_path.exists():
        try:
            with open(orgs_json_path, "r", encoding="utf-8") as f:
                active_orgs = json.load(f)
            if isinstance(active_orgs, list):
                print(f"  - 发现网关激活账号数: {len(active_orgs)}")
                for idx, o in enumerate(active_orgs, 1):
                    print(f"    {idx}. 域名: {o.get('domain_name')} (ID: {o.get('id')})")
            else:
                print("  - [WARN] orgs.json 格式非列表！")
        except Exception as e:
            print(f"  - [WARN] 读取 orgs.json 失败: {e}")
    else:
        print("  - [WARN] 未找到 orgs.json 文件，网关目前没有导入任何账号！")
        
    print("-" * 50)
    print("诊断完毕！")

if __name__ == "__main__":
    main()
