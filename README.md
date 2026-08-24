# AI Signal Research

一个面向 AI 二级主观投研的 Agent-first 信息系统。仓库负责把 Substack、Reddit、X 和 Yahoo Finance 采集成可审计的统一 JSON；用户自己的 Agent 再读取 JSON 和个人研究画像，生成日报或周报。

它借鉴了 [ai-signal](https://github.com/hhhhhhhqa/ai-signal) 的关键边界：中央采集层只提供原料，最终判断留在 Agent 侧。本项目没有直接复制原仓库，而是针对二级投研增加了统一事件模型、股票映射、价格验证、事实/推断分离和证伪条件。

## 当前状态

四条 pipeline 已实现：Substack 读取公开 RSS；Reddit 优先使用 OAuth，未配账号时自动尝试公开 JSON 和 RSS；X 支持官方 API v2，也允许为单个账号配置自有 RSS 地址；Yahoo Finance 采集日线并计算 1 日、5 日、21 日收益和距 252 日高点的距离。X 的代码和测试已经打通，但在加入账号与 `X_BEARER_TOKEN`（或 `rss_url`）前会明确标记为 `not_configured`/`needs_credentials`，不会伪造空成功。

所有来源归一为同一个 `SignalItem`：稳定 ID、来源类型、来源名称、作者、标题、正文、原始链接、发布时间、采集时间、关联股票、互动数据和来源特有 metadata。每次采集同时写入滚动最新页 `data/feeds/latest.json` 与按日快照 `data/snapshots/YYYY-MM-DD.json`，周报因此可以跨多日读取而不依赖外部数据库。

## 快速开始

需要 Python 3.11 或更高版本：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

ai-signal doctor
ai-signal collect
ai-signal prepare --period daily
ai-signal prepare --period weekly
```

生成完成后，让 Agent 读取 `reports/contexts/daily-agent-prompt.md` 或 `weekly-agent-prompt.md`。提示词会指向对应的结构化 context；context 已经根据用户画像过滤、打分并标注主题和潜在关联标的。

如只想调试一条链路，可以运行：

```bash
ai-signal collect --pipeline substack
ai-signal collect --pipeline reddit
ai-signal collect --pipeline x
ai-signal collect --pipeline prices
```

单独刷新某条 pipeline 时，程序会保留 `latest.json` 里其他来源的已有数据。加入 `--strict` 后，任何已配置 pipeline 的真实错误都会让命令非零退出，适合 CI 健康检查。

## 个性化方式

`config/sources.yaml` 只定义“看哪些源”；`config/profiles/default.yaml` 定义“用户怎么看”。后者可以配置语言、日报/周报时间窗、条目上限、主题关键词、黑白名单、股票 watchlist 与别名、不同来源的基础权重。建议每个用户新建一份 profile，而不是修改采集层。

日报和周报 prompt 不要求 Agent 做新闻摘要，而是要求它回答：什么预期发生了变化，变化如何传导到上市公司，价格是否确认，以及什么证据会推翻这个 idea。Reddit/X 单条内容只能作为线索，不能自动升级为已证实事实。

## 账号与 Secrets

复制 `.env.example` 可看到所需变量。Reddit 匿名抓取现在即可运行，加入 `REDDIT_CLIENT_ID`、`REDDIT_CLIENT_SECRET` 和明确的 `REDDIT_USER_AGENT` 后会自动切换 OAuth。X 官方路径需要 `X_BEARER_TOKEN`；如果你有自建 RSSHub/Nitter 类服务，也可以直接给账号填 `rss_url`。

GitHub Actions 每天香港时间 06:00 运行测试、采集、准备日报和周报 context，并把数据提交回仓库。账号加入后，把同名变量写入仓库 Settings → Secrets and variables → Actions 即可，不需要改 workflow。

## 目录

```text
config/                 来源和用户画像
data/feeds/             最新统一 feed
data/snapshots/         按日快照，供周报聚合
prompts/                日报/周报 Agent 指令
reports/contexts/       Agent 可直接读取的输入包
src/ai_signal/pipelines 四条采集链路
SKILL.md                Agent 使用入口
```

## 已知边界

公开 Reddit 端点可能按地区或 IP 限流，OAuth 是更稳定的长期方式。X 没有稳定且合规的通用匿名接口，因此仓库不会内置脆弱的网页抓取；账号未配置时会把缺口暴露给 Agent。Yahoo Finance 适合 idea generation 的价格上下文，不应替代交易、复权核对或正式行情源。
