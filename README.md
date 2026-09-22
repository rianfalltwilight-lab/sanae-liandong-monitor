# 链动小铺监控 / 早苗

独立 Python 侧进程，当前验证环境为 Python 3.14，只使用标准库。Windows 计划任务负责常驻运行，现有早苗/LLBot 负责 QQ 通道。源码仓库包含离线测试和脱敏样本；当前生产配置、凭据和现场记录留在本机。

监控用户指定的六家已确认店铺，并加入麻豆科技上货货架，匹配所有价格的短时速刷 team5x。QQ 群 **1092470719** 只接收特惠车提醒（有货、单价严格低于 ¥40、明确提供 2FA，并且是新上架/补货或进入最后五个）；全部普通上架和最后五个提醒仍独立私发给 QQ **2731538103**，特惠车同时私发。两路分别持久化回执，私聊失败不会重发已成功的群消息。上架包括新商品、重新上架、售罄后补货；最后五个指库存从大于 5 降到 1–5，跨过门槛时只提醒一次。每条通知附公开购买链接及本轮健康店铺中已知有货商品的最低单价。不同套餐时长不等，最低单价不等于最低每小时价格。

- 默认每 20 秒检查五店完整公开目录，并轮询麻豆科技 `https://mdkj.team/shop/api/third-party` 的第三方上货货架。麻豆接口使用本机 `token_file` 读取 `X-Customer-Token`，只读不下单；价格按服务端时间和整分钟动态计算。请求超时、限流或字段结构不符时保留上次状态，单店指数退避至最多 10 分钟，不误判下架。
- 确定的速刷标题按规则立刻通知；含义不清的 team5x 标题由现有 DeepSeek API 分类，成功结果按标题/描述缓存。模型失败不阻塞确定商品通知。
- 商品提醒同步读取 OpenAI 公共状态页的只读 JSON（默认每 60 秒缓存一次）。状态采集失败或状态页报告异常时，提醒明确标为 `本时段不推荐买`；状态采集成功且整体及组件均正常时标为 `本时段可以考虑买`。这只是服务可用性参考，不是库存、账号质量或成交保证。Claude 状态解析保留为可选配置，默认不请求。
- 实际价格、库存、链接来自商品接口，模型不能改写这些字段。未知库存单独标注，不参与“有货最低价”。
- 库存 8→5 或 8→3 提醒；5→4→3 不重复；直接售罄、价格变化、一直有货时的补量、普通库存变化和下架均静默。售罄后 0→正数按上架提醒。升级保留商品基线并丢弃旧规则待发消息，不重发已有目录。
- **醒目提醒**：上述两类通知中的商品若有货、单价严格低于 ¥40 且明确提供 2FA，显示 `🚨【低价2FA速刷提醒｜低于¥40】` 并真实艾特 **3294692833、1920924896**。艾特规则不独立产生降价或普通补量消息。¥40 整、库存未知/无货、未明确提供 2FA 均不触发艾特。
- OneBot 固定目标群，商品内容使用纯文字段，只有上述规则可附加两位固定账号的 `at` 消息段，商家标题不能插入 CQ 指令。HTTP 成功且业务成功并返回 message_id 才记为已发。
- 状态、待发消息和回执持久化；失败短期重试，超过 120 秒或内容已变化的通知按原事件类型刷新价格/库存。最后五个通知在补量到 5 个以上或售罄后取消；迟发上架消息标记“延迟送达”。QQ 不提供幂等发送事务，极端情况下发送成功后进程立即崩溃，重启可能重复一次。
- 独立进程复用早苗的 Python、DeepSeek 凭据、LLBot OneBot，现有早苗主桥接不需要重启或改配置。凭据仅在运行时读取，不复制进本项目。运行前将已登录 mdkj 工作空间生成的客户 API Token 保存到配置中的 `token_file`，文件不进入 Git；Edge 会话 Cookie 不会被监控器读取。

## 启停

监控店铺：RenWin、咕咕嘎嘎、HZ-API、[GPT的爷](https://catfk.com/shop/GPTsgrandpa)，以及 [商家5297](https://wzyp.cn/shop/chenrui19)。麻豆科技作为 `kind: "mdkj"` 单独使用 `token_file`，购买入口为 [第三方货源](https://mdkj.team/shop/?view=third-party)。商家6635已从配置中移除，不再采集、提醒或纳入新日报；已有历史数据库和状态记录保留用于追溯。`config.example.json` 包含当前名单；GPT的爷使用 `base_url: "https://catfk.com"`，购买链接保留该域名。其余店铺默认 `https://wzyp.cn`，仅接受这两个已核验站点。新增店铺沿用现有状态文件，已有店铺不会因此重新发送初始通知。

首次从仓库使用：

```powershell
Copy-Item config.example.json config.json
python -X utf8 -m unittest discover -s tests -v
python -X utf8 shop_monitor.py --once --dry-run
```

将 `config.json` 的 `sanae_root` 指向早苗目录，那里现有的 `secrets.json` 需提供 `deepseek_api_key`；也可用 `--no-ai` 预览仅规则匹配的结果。`config.json` 和凭据都不会进入 Git。离线测试不需要任何凭据，也不会向 QQ 或 DeepSeek 发请求。

本私有部署固定 QQ 群 **1092470719** 及两位艾特对象，代码有对应限制。改用其它群时须同时调整 `shop_monitor.py` 的目标限制和配置。`install-monitor.ps1`、`start-monitor.ps1`、`stop-monitor.ps1` 面向下述 Windows 部署路径；在其它机器使用前需调整脚本路径。`--dry-run` 只生成预览；省略它会使用实际发群通道。

部署位置：`E:\Minecarft\.E3_LLBot_Sanae\liandong-monitor`。
计划任务：`Sanae Liandong Shop Monitor`，随 Monarch 登录启动，异常退出由计划任务重启。

```powershell
Start-ScheduledTask -TaskName 'Sanae Liandong Shop Monitor'
Get-ScheduledTask -TaskName 'Sanae Liandong Shop Monitor'
& 'E:\Minecarft\.E3_LLBot_Sanae\liandong-monitor\stop-monitor.ps1'
```

暂停脚本会禁用并停止此任务，保留状态/日志。重新启用：

```powershell
Enable-ScheduledTask -TaskName 'Sanae Liandong Shop Monitor'
Start-ScheduledTask -TaskName 'Sanae Liandong Shop Monitor'
```

卸载任务并保留证据：`stop-monitor.ps1 -RemoveTask`。不影响原 `Sanae LLBot Bridge` 任务。

## 每日价格数据、分析和图表

开启 `analytics.enabled` 后复用每次监控结果，写入独立的 `state/analytics.sqlite3`。当前六家店分别记录采集成功或失败、已确认有货目标商品数和最低价；商品记录保存价格、库存、上架状态、来源链接及版本。价格变化、库存变化及时留档，未变商品每 5 分钟保留心跳。历史从启用时开始，不能恢复启用前的走势。

每小时刷新当天预览；北京时间每天 **00:05** 生成前一天日报。默认只生成本地文件；本机已授权部署使用 `analytics.send_daily: true`，把摘要、PNG 图表和 Excel 分别发送到群 **1092470719** 与私聊 **2731538103**。摘要会带上日报最后一次 OpenAI 状态判断。当天预览不推送。主机停机后恢复会补发最近 3 天中实际有记录且未完成的日报，更早日期可手动导出。六个发送步骤单独保存回执，失败 5 分钟后补齐；QQ 不提供跨进程幂等事务，远端已接收但回执丢失时仍可能重复。

输出保存在 `reports/YYYY-MM-DD/`：

- `index.html`：离线日表、各店铺及每个商品版本的走势图、事实分析，可展开查看商品；图表可另存 SVG。
- `daily.xlsx`：日报、商品明细、价格采样、原始观测、图表五张工作表；图表可编辑，首末变化和采集成功率为公式。
- `chart.png`：供 QQ 转发的店铺比较图；`charts/` 保存总览、各店铺和商品版本 SVG。
- `shops.csv`、`products.csv`、`series.csv`、`observations.csv`、`report.json`、`analysis.md`：完整明细与分析，可用于进一步处理。CSV 对可能被表格程序当作公式的文本加前导单引号。

**统计口径**：店铺曲线是每轮有货目标商品的最低标价，最低价商品可能变化；商品曲线按商品 ID 与标题、描述指纹分版本。同款价格变化和店铺最低价变化分别统计。不同套餐的时长、配置和服务不等，标价不能直接换算成性价比。库存下降不视为销量。失败、未知库存、待分类和无有效价格不填成 0；缺失时段断线，日报明确标注覆盖不足。分析由已记录数据计算，不由模型编造行情。

采集、HTML、CSV、SVG 和分析仅需 Python 标准库。Excel 与 PNG 导出使用 Node.js 和 `@oai/artifact-tool`，需在 `analytics.xlsx.node_executable` 填 Node 完整路径，在 `artifact_tool_module` 填已安装包的 `dist/artifact_tool.mjs` 完整路径（也可让 Node 正常解析该包）。本项目不分发这两个运行时；缺少它们仍可生成 HTML/CSV/SVG。启用 QQ 日报推送时须先配置并验证 Excel/PNG 导出。运行配置、数据库、日报与包依赖均不进入 Git。

手动导出指定日期（不发 QQ）：

```powershell
python -X utf8 analytics_report.py --database state/analytics.sqlite3 --date 2026-09-21 --output reports
node export_daily_xlsx.mjs --input reports/2026-09-21/report.json --output reports/2026-09-21/daily.xlsx --chart-png reports/2026-09-21/chart.png
```

导出在独立后台进程进行，不阻塞商品提醒；按日期加跨进程锁，避免重启时重复发报。`state/report-jobs/` 保存每日进度，`state/analytics-export.log` 保存导出输出。`--dry-run` 不写入统计库，也不生成或推送日报。启用后可在 `state/monitor.json` 检查 `analytics_error`，并核对数据库记录持续增长。

## 验证与证据

`tests/` 为离线行为测试，包含 `tests/fixtures/catalog/` 脱敏样本；GitHub Actions 在 Windows/Linux 运行测试并在 Windows 校验 PowerShell 语法。`evidence/` 是本地生成且被忽略的验收目录。运行状态在部署目录的 `state/monitor.json`，日志 `state/monitor.log` 限 2 MB、3 个轮换副本。`state/process.json` 记录当前进程身份，`installation.json` 记录安装文件哈希。

```powershell
& 'E:\Minecarft\.E3_LLBot_Sanae\.venv\Scripts\python.exe' -X utf8 -m unittest discover -s tests -v
& 'E:\Minecarft\.E3_LLBot_Sanae\.venv\Scripts\python.exe' -X utf8 shop_monitor.py --once --dry-run --state evidence/preview-state.json
.\install-monitor.ps1
.\install-monitor.ps1 -Apply
```

首次上线以真实目录生成通知并核对返回 message_id；手动购买由用户在商品页面完成。轮询检测时延通常为一轮加网络时间，不承诺即时、不保证库存保留。主机休眠/关机或 QQ 离线期间不能即时通知。登录自启设置与实际冷启动验收分别记录。

DeepSeek JSON 输出协议依据：[官方文档](https://api-docs.deepseek.com/guides/json_mode/)。模型名以本轮认证 `/models` 结果核验，使用 `deepseek-flash`。
