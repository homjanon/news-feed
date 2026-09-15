# 老张新闻源（news-feed）

独立的每日新闻抓取服务：**谷歌新闻（Business·美国区）+ 联合早报（中港台即时）各取最新 10 条 = 20 条**，每天下午（15:20）、夜间（22:30）各抓一场，产出静态 JSON（供「老张工具箱」App 读取）。

> 与 `portfolio` 仓完全解耦：portfolio 的每日 06:30 日报不受本项目任何影响；本项目也**不依赖** portfolio。

## 为什么独立建仓

- **下午茶 / 夜豆浆双场次**：portfolio 的日报是"隔夜+今晨"语义，加"下午场"要动它的模式判定，风险大。独立抓取则一天两场天然成立。
- **App 数据源**：老张工具箱 App（一期）读这里的 JSON 渲染新闻流，阅读统一在「老张工具箱」App 内（网页阅读页已于 2026-09-15 下线）。
- **可扩展**：后续新增 RSS 只改 `scripts/sources.json`，不改代码。

## 数据流

```
触发（Cloudflare Worker qdii-dispatch 心跳 → workflow_dispatch）
        ↓
scripts/fetch_news.py --edition afternoon|night
        ├─ 抓谷歌 Business 美国区（62 条 → 去重 → 按 pubDate 倒序 → 取 10；失败换英国区）
        ├─ 抓联合早报中港台即时（三实例兜底 → 取 10）
        ├─ LLM 中文化谷歌词（Agnes → Gemini 链；失败降级英文原标题，绝不空窗）
        └─ 写 docs/news/{日期}-{场次}.json + docs/latest.json（保留 7 天归档）
        ↓
docs/（数据存放）→ App 经 Cloudflare 代理读 raw（Pages 已下线）
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

| 场次 | 北京时间 | edition | 内容 |
|---|---|---|---|
| 🍵 下午茶 | 15:20 | `afternoon` | 谷歌最新 10 + 早报最新 10 |
| 🥛 夜豆浆 | 22:30 | `night` | 同上（含上一场之后的增量，标「新」） |

**触发方式（单通道 · 2026-09-13 起）**：GitHub 侧已**移除 schedule**，只由 Cloudflare Worker `qdii-dispatch` 定时调用（心跳每 5 分钟）：

端点与参数：
   ```
   POST https://api.github.com/repos/homjanon/news-feed/actions/workflows/fetch.yml/dispatches
   Authorization: Bearer <GITHUB_TOKEN>      # 需 repo + workflow 权限
   {"ref":"main","inputs":{"edition":"afternoon"}}   # 下午茶
   {"ref":"main","inputs":{"edition":"night"}}       # 夜豆浆
   ```

## Secrets（用户自管，与 portfolio 同名）

| Secret | 用途 | 缺失时 |
|---|---|---|
| `AGNES_API_KEY` | 谷歌词中文化主模型 | 降级英文标题 |
| `GEMINI_API_KEY` | 第②层 | 同上 |

## 输出格式

```json
{
  "edition": "afternoon",        // afternoon=下午茶 / night=夜豆浆
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

App 端按 `block` 自动配色（网页阅读页已下线）。

## 本地调试

```bash
# 谷歌源本机被墙时走代理；未配置 LLM Key 会自动降级英文标题
HTTPS_PROXY=http://127.0.0.1:7890 python scripts/fetch_news.py --edition afternoon --outdir docs
```

## 页面

- 阅读：**已迁至「老张工具箱」App**（网页阅读页 `docs/index.html` 于 2026-09-15 下线）
- 数据：`docs/latest.json`（最新一场）、`docs/news/{日期}-{场次}.json`（7 天归档）

免责声明：内容来自公开 RSS，仅供研究参考，不构成投资建议。

---

## ⚠️ 2026-09-15 变更（请以此为准）

1. **触发方式**：已**取消 GitHub Actions 定时**，只由 **Cloudflare Worker 定时调用**
   （保留手动 Run workflow）。两个场次与参数：
   | 时间（北京） | 场次 | inputs |
   |---|---|---|
   | 15:20 | 下午茶 | `{"ref":"main","inputs":{"edition":"afternoon"}}` |
   | 22:30 | 夜豆浆 | `{"ref":"main","inputs":{"edition":"night"}}` |
   端点：`POST /repos/homjanon/news-feed/actions/workflows/fetch.yml/dispatches`

2. **网页阅读页已下线**：`docs/index.html` 已删除，本仓只作为**数据存放**（`docs/latest.json`、`docs/news/*.json`）。
   阅读统一在「老张工具箱」App 内进行。

3. **数据读取地址（App 用）**：GitHub Pages 不再发布，改为经 Cloudflare 代理读 raw：
   ```
   https://proxy.hellohopo.dpdns.org/?url=<encodeURIComponent(
       https://raw.githubusercontent.com/homjanon/news-feed/main/docs/latest.json )>
   ```
   单场文件：`.../main/docs/news/{日期}-{afternoon|night}.json`

4. **来源字段**：所有条目都有 `source`；联合早报源在 `sources.json` 里配了 `default_source: 联合早报`。

5. **LLM 调用**：全场**只调一次**（合并两个源），整批失败会自动拆 2 批重试，仍失败才降级为 RSS 原文。
