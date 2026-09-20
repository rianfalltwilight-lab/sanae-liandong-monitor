# 链动小铺监控 / 早苗

独立 Python 侧进程，当前验证环境为 Python 3.14，只使用标准库。Windows 计划任务负责常驻运行，现有早苗/LLBot 负责 QQ 通道。源码仓库包含离线测试和脱敏样本；当前生产配置、凭据和现场记录留在本机。

监控用户指定的五家店铺，匹配所有价格的短时速刷 team5x，向 QQ 群 **1092470719** 仅发送上架和最后五个提醒。上架包括新商品、重新上架、售罄后补货；最后五个指库存从大于 5 降到 1–5，跨过门槛时只提醒一次。每条通知附公开购买链接及本轮健康店铺中已知有货商品的最低单价。不同套餐时长不等，最低单价不等于最低每小时价格。

- 默认每 20 秒检查五店完整公开目录。请求超时、限流或字段结构不符时保留上次状态，单店指数退避至最多 10 分钟，不误判下架。
- 确定的速刷标题按规则立刻通知；含义不清的 team5x 标题由现有 DeepSeek API 分类，成功结果按标题/描述缓存。模型失败不阻塞确定商品通知。
- 实际价格、库存、链接来自商品接口，模型不能改写这些字段。未知库存单独标注，不参与“有货最低价”。
- 库存 8→5 或 8→3 提醒；5→4→3 不重复；直接售罄、价格变化、一直有货时的补量、普通库存变化和下架均静默。售罄后 0→正数按上架提醒。升级保留商品基线并丢弃旧规则待发消息，不重发已有目录。
- **醒目提醒**：上述两类通知中的商品若有货、单价严格低于 ¥40 且明确提供 2FA，显示 `🚨【低价2FA速刷提醒｜低于¥40】` 并真实艾特 **3294692833、1920924896**。艾特规则不独立产生降价或普通补量消息。¥40 整、库存未知/无货、未明确提供 2FA 均不触发艾特。
- OneBot 固定目标群，商品内容使用纯文字段，只有上述规则可附加两位固定账号的 `at` 消息段，商家标题不能插入 CQ 指令。HTTP 成功且业务成功并返回 message_id 才记为已发。
- 状态、待发消息和回执持久化；失败短期重试，超过 120 秒或内容已变化的通知按原事件类型刷新价格/库存。最后五个通知在补量到 5 个以上或售罄后取消；迟发上架消息标记“延迟送达”。QQ 不提供幂等发送事务，极端情况下发送成功后进程立即崩溃，重启可能重复一次。
- 独立进程复用早苗的 Python、DeepSeek 凭据、LLBot OneBot，现有早苗主桥接不需要重启或改配置。凭据仅在运行时读取，不复制进本项目。

## 启停

监控店铺：商家6635、RenWin、咕咕嘎嘎、HZ-API，以及 [GPT的爷](https://catfk.com/shop/GPTsgrandpa)。`config.example.json` 包含完整名单；GPT的爷使用 `base_url: "https://catfk.com"`，购买链接保留该域名。其余店铺默认 `https://wzyp.cn`，仅接受这两个已核验站点。新增店铺沿用现有状态文件，已有店铺不会因此重新发送初始通知。

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
