import json
import os
import re
import urllib.request
from pathlib import Path

def request_retool(method: str, url: str, access_token: str, xsrf_token: str, payload: dict = None) -> dict:
    req = urllib.request.Request(url, method=method)
    req.add_header("accept", "application/json")
    req.add_header("content-type", "application/json")
    req.add_header("cookie", f"accessToken={access_token}; xsrfToken={xsrf_token}")
    req.add_header("x-xsrf-token", xsrf_token)
    
    data = None
    if payload:
        data = json.dumps(payload).encode("utf-8")
        
    try:
        with urllib.request.urlopen(req, data=data) as response:
            res_body = response.read().decode("utf-8")
            return json.loads(res_body)
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
    print("诊断完毕！")

if __name__ == "__main__":
    main()
