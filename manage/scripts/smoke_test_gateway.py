import argparse
import json
import sys
from pathlib import Path

import httpx


def fail(message: str):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def configure_stdio_for_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")


def main():
    configure_stdio_for_utf8()
    parser = argparse.ArgumentParser(description="Smoke test the Retool OpenAI gateway")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Gateway base URL")
    parser.add_argument("--api-key", required=True, help="Internal API key")
    parser.add_argument("--model", required=True, help="Model alias to call")
    parser.add_argument("--conversation-id", required=True, help="Conversation ID for thread reuse")
    parser.add_argument("--message", default="测试连接，请简短回复。", help="Prompt message")
    parser.add_argument(
        "--expect-substring",
        default="",
        help="Optional substring expected in the final assistant response",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {args.api_key}",
        "Content-Type": "application/json",
        "X-Conversation-ID": args.conversation_id,
    }

    with httpx.Client(timeout=180.0) as client:
        models_response = client.get(f"{base_url}/v1/models", headers=headers)
        if models_response.status_code != 200:
            fail(f"/v1/models failed: {models_response.status_code} {models_response.text}")
        print("Models:", models_response.text)

        payload = {
            "model": args.model,
            "messages": [{"role": "user", "content": args.message}],
            "stream": False,
        }
        response = client.post(f"{base_url}/v1/chat/completions", headers=headers, content=json.dumps(payload))
        if response.status_code != 200:
            fail(f"/v1/chat/completions failed: {response.status_code} {response.text}")

        body = response.json()
        content = body["choices"][0]["message"]["content"]
        print("Assistant response:", content)

        if args.expect_substring and args.expect_substring not in content:
            fail(f"Assistant response missing expected substring: {args.expect_substring}")

    print("Smoke test passed.")


if __name__ == "__main__":
    main()
