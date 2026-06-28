# retoolautoregautomange

`retoolautoregautomange` 是一个面向 Retool 的注册与采集项目根目录。

它的职责不是只做网关，而是覆盖两部分：

- 外层注册/采集端
  - 负责账号注册自动化
  - 负责浏览器登录态采集
  - 负责为后续管理端准备账号与会话数据
- 内层管理端 `manage/`
  - 负责账号库存管理
  - 负责 org 会话池管理
  - 负责 OpenAI 兼容接口与管理页面

## 目录结构

核心代码位于：

- [manage](C:/Users/Administrator/Desktop/retoolautoregautomange/manage)

## 核心能力

- Retool 账号注册自动化
- 浏览器登录态采集与落盘
- 账号库存与组织会话池管理
- OpenAI 兼容接口
- Claude Code 兼容入口
- 管理页面与导入/刷新流程

## 快速开始

注册脚本与管理子系统都在当前仓库内。

如果要运行管理端：

```bash
cd manage
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

详细说明见：

- [manage/README.md](C:/Users/Administrator/Desktop/retoolautoregautomange/manage/README.md)

如果要运行外层注册脚本，请直接使用根目录下的：

- [singup.py](C:/Users/Administrator/Desktop/retoolautoregautomange/singup.py)
- [signup_playwright.py](C:/Users/Administrator/Desktop/retoolautoregautomange/signup_playwright.py)

## 开源说明

- 当前仓库保留完整源码
- 已移除本地运行态、真实账号、真实 key、真实 session 与历史仓库元数据
- 需要在正式发布前补充最终 `LICENSE`

## 社区发布

如果用于 LINUX DO 社区开源推广，建议同时准备：

- 项目介绍正文
- AI 辅助生成/润色说明截图
- 最终 License
- 首个公开版本 tag 或 release
