import json
import os
import re
import time
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
            print("错误详情:", e.read().decode("utf-8")[:2000])
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
    print("-" * 50)
    
    # 获取所有的 agents
    agents_data = request_retool("GET", f"{workspace_url}/api/agents", access_token, xsrf_token)
    agents = agents_data.get("agents", [])
    
    gpt_agent = None
    for agent in agents:
        if agent.get("name") == "gpt":
            gpt_agent = agent
            break
            
    if not gpt_agent:
        print("未在当前空间中找到 gpt 机器人！")
        return
        
    agent_id = gpt_agent["id"]
    print(f"定位到 gpt 机器人 UUID: {agent_id}")
    
    # 1. 创建 Thread
    print("正在创建测试对话 Thread...")
    thread_data = request_retool(
        "POST",
        f"{workspace_url}/api/agents/{agent_id}/threads",
        access_token,
        xsrf_token,
        payload={"name": "debug-conversation", "timezone": "Asia/Shanghai"}
    )
    thread_id = thread_data.get("id")
    if not thread_id:
        print("创建 Thread 失败！")
        return
    print(f"创建 Thread 成功, ID: {thread_id}")
    
    # 2. 发送消息
    message = "Hello, response with 1 word."
    print(f"正在发送消息: {message}")
    msg_data = request_retool(
        "POST",
        f"{workspace_url}/api/agents/{agent_id}/threads/{thread_id}/messages",
        access_token,
        xsrf_token,
        payload={"type": "text", "text": message, "timezone": "Asia/Shanghai"}
    )
    run_id = msg_data.get("content", {}).get("runId")
    if not run_id:
        print("发送消息失败, 未返回 runId! 完整响应:")
        print(json.dumps(msg_data, indent=2, ensure_ascii=False))
        return
    print(f"发送消息成功, 启动的 Run ID: {run_id}")
    
    # 3. 轮询日志并打印完整数据
    print("正在轮询 Run 状态与日志...")
    last_log_uuid = "00000000-0000-7000-8000-000000000000"
    for i in range(15):
        time.sleep(2)
        log_url = f"{workspace_url}/api/agents/{agent_id}/logs/{run_id}?startAfterUUID={last_log_uuid}&limit=100"
        log_data = request_retool("GET", log_url, access_token, xsrf_token)
        
        status = log_data.get("status")
        print(f"  [第 {i+1} 次查询] 状态: {status}")
        
        pagination = log_data.get("pagination") or {}
        if pagination.get("lastLogUUID"):
            last_log_uuid = pagination["lastLogUUID"]
            
        if status in ("COMPLETED", "FAILED"):
            print("-" * 50)
            print("Run 结束！完整 JSON 响应结构诊断:")
            print(json.dumps(log_data, indent=2, ensure_ascii=False))
            print("-" * 50)
            
            # 提取 content
            trace = log_data.get("trace") or []
            content_found = False
            for span in reversed(trace):
                content = span.get("data", {}).get("data", {}).get("content")
                if content:
                    print(f"提取到的助手最终回复: {content}")
                    content_found = True
                    break
            if not content_found:
                print("[!] 未在 trace 中找到任何 content 数据！")
            return
            
    print("轮询超时，未能在 30 秒内完成任务。")

if __name__ == "__main__":
    main()
