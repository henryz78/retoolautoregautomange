# retoolautoregautomange

`retoolautoregautomange` 是一个围绕 Retool 的自动化项目，重点是`自动注册`、`自动采集`和`自动管理`。

仓库分为两层：

- 根目录：注册自动化与登录态采集端
- [manage](./manage)：账号管理、会话池管理和兼容 API 网关

## 项目定位

这个仓库首先解决的是`自动化`问题，不是单纯做一个网关页面。

它覆盖的链路是：

1. 自动注册 Retool 账号
2. 自动驱动浏览器完成登录态采集
3. 自动整理账号、组织子域名和会话数据
4. 将采集结果交给 `manage/` 做统一管理和 API 对外提供

## 核心能力

- Retool 账号注册自动化
- 浏览器登录态与组织会话自动采集
- 账号库存与会话池管理
- OpenAI 兼容接口
- Claude Code 兼容入口
- 管理页面与导入、刷新流程

## 目录说明

- [singup.py](./singup.py)：根目录注册自动化脚本
- [signup_playwright.py](./signup_playwright.py)：基于 Playwright 的注册流程实现
- [manage](./manage)：管理端与兼容网关子系统
- [manage/README.md](./manage/README.md)：管理端详细说明

## 快速开始

如果你要关注的是自动化注册与采集，请先看根目录脚本：

- [singup.py](./singup.py)
- [signup_playwright.py](./signup_playwright.py)

如果你要运行管理端：

```bash
cd manage
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

## 适用场景

- 批量自动注册 Retool 账号
- 批量采集浏览器登录态和组织会话
- 将自动化采集结果统一接入内部管理端
- 为后续网关调用准备结构化账号数据

## License

MIT
