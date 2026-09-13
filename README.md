# 老张新闻源（news-feed）

独立的每日新闻抓取服务：**谷歌新闻（Business·美国区）+ 联合早报（中港台即时）各取最新 10 条 = 20 条**，每天早（07:00）、下午（15:20）各抓一场，产出静态 JSON + 手机友好的阅读页。

> 与 `portfolio` 仓完全解耦：portfolio 的每日 06:30 日报不受本项目任何影响；本项目也**不依赖** portfolio。

## 为什么独立建仓

- **早咖啡 / 下午茶双场次**：portfolio 的日报是"隔夜+今晨"语义，加"下午场"要动它的模式判定，风险大。独立抓取则一天两场天然成立。
- **App 数据源**：老张工具箱 App（一期）读这里的 JSON 渲染新闻流，网页照旧读 portfolio——一份数据两个出口，互不干扰。
- **可扩展**：后续新增 RSS 只改 `scripts/sources.json`，不改代码。

## 数据流

```
触发（Actions schedule 或 Cloudflare workflow_dispatch）
        ↓
scripts/fetch_news.py --edition morning|afternoon
        ├─ 抓谷歌 Business 美国区（62 条 → 去重 → 按 pubDate 倒序 → 取 10；失败换英国区）
        ├─ 抓联合早报中港台即时（三实例兜底 → 取 10）
        ├─ LLM 中文化谷歌词（Agnes → Gemini 链；失败降级英文原标题，绝不空窗）
        └─ 写 docs/news/{日期}-{场次}.json + docs/latest.json（保留 7 天归档）
        ↓
GitHub Pages（/docs）→ https://homjanon.github.io/news-feed/
```

## 关键设计（踩过的坑）

| 规则 | 原因 |
|---|---|
| **必须按 pubDate 倒序后再取前 N** | 谷歌 feed 顺序是"热度混合"不按时间（实测第1条 04:54、第2条 07:54），直接取前 10 会混入旧闻 |
| **选条不用 LLM** | "最新 10 条"是硬规则要可复现；LLM 只做译标题+摘要 |
| **翻译失败降级英文** | 翻译链（Agnes/Gemini）任一环节挂掉都不能让整场空窗 |
| **抓取失败不覆盖旧数据** | 本场全失败时脚本退出码 1 且不写文件，`latest.json` 保持上一场内容 |
| **两块独立不补位** | 谷歌挂了就是少一块，不用早报凑 20 条（与 portfolio Top20 口径一致） |

## 场次与触发

| 场次 | 北京时间 | 内容 |
|---|---|---|
| ☕ 早咖啡 | 07:00 | 谷歌最新 10 + 早报最新 10 |
| 🍵 下午茶 | 15:20 | 同上（含上午之后的增量，标「新」） |

**触发方式（单通道 · 2026-09-13 起）**：由 **Cloudflare Worker `qdii-dispatch`** 统一调度
——心跳每 5 分钟，到点调 GitHub API `workflow_dispatch` 触发本仓 `fetch.yml`：

```
POST https://api.github.com/repos/homjanon/news-feed/actions/workflows/fetch.yml/dispatches
Authorization: Bearer <GITHUB_TOKEN>      # 需 repo + workflow 权限
{"ref":"main","inputs":{"edition":"morning"}}    # 或 "afternoon"
```

> GitHub 侧已**移除 schedule**（不再自触发）：Actions 共享 cron 队列会偶发延迟/静默跳过。
> 手动补跑任一指定场次：
> `https://qdii-dispatch.homjanon.workers.dev/trigger?repo=news-feed&edition=afternoon&key=<DISPATCH_KEY>`

## Secrets（用户自管，与 portfolio 同名）

| Secret | 用途 | 缺失时 |
|---|---|---|
| `AGNES_API_KEY` | 谷歌词中文化主模型 | 降级英文标题 |
| `GEMINI_API_KEY` | 第②层 | 同上 |

## 输出格式

```json
{
  "edition": "morning",          // morning=早咖啡 / afternoon=下午茶
  "date": "2026-09-12",
  "fetched_at": "2026-09-12 07:04",
  "translator": "agnes",         // none(原文)=未配置 Key 或翻译失败
  "counts": { "total": 20, "new": 8 },
  "items": [{
    "title": "标普500收涨，CPI强化加息预期",
    "summary": "美国8月CPI高于预期，交易员定价9月加息概率约90%",
    "source": "Reuters",
    "url": "https://news.google.com/...",
    "pubTime": "09-12 07:19",
    "block": "谷歌精选",          // 或 联合早报
    "isNew": true                 // 与上一场比对
  }]
}
```

## 如何新增 RSS 源

只改 `scripts/sources.json` 的 `sources` 数组，加一个对象：

```json
{
  "id": "my-new-source",
  "block": "显示名",
  "take": 10,
  "dedupe": true,
  "sort_desc": true,             // feed 顺序不可信就开（谷歌系必须开）
  "fresh_check": false,          // 拦旧缓存镜像（早报类建议开）
  "title_suffix_source": false,  // 「标题 - 来源」后缀拆媒体名（谷歌系才需要）
  "translate": false,            // 英文源才开
  "urls": ["https://…", "https://…备用实例"]
}
```

前端阅读页无需改动（按 `block` 自动配色）。

## 本地调试

```bash
# 谷歌源本机被墙时走代理；未配置 LLM Key 会自动降级英文标题
HTTPS_PROXY=http://127.0.0.1:7890 python scripts/fetch_news.py --edition morning --outdir docs
```

## 页面

- 阅读页：`docs/index.html`（早咖啡/下午茶双页签，15:00 后自动切下午茶）
- 数据：`docs/latest.json`（最新一场）、`docs/news/{日期}-{场次}.json`（7 天归档）

免责声明：内容来自公开 RSS，仅供研究参考，不构成投资建议。
