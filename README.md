感谢作者：https://github.com/oyxning/astrbot_plugin_zirunbi给我的灵感，但是ui有点不太方便看所以参考了一下
感谢dsh大肥鱼参与编写，此插件100%ai编写，我作为项目经理审批（bushi）
此项目为mit协议  mit省流：你爱干啥干啥留个我名就行，但我不担责（话糙理不糙）
此readme并非最新，以压缩包内为准！
此readme并非最新，以压缩包内为准！
此readme并非最新，以压缩包内为准！
# astrbot_plugin_market_pro
astrbot的模拟炒股插件来cos久留美吧！多币种模拟交易插件 ：交易所风格 Web 交易端（含免责声明、杠杆 1-125x、逐仓保证金与强平），每个币种可单独设置走势，历史成交含现货与合约，支持一键添加币种、管理员加钱/转账/转股，以及带二次确认的一键重置股价（插件页 + 配置页）。纯模拟，不涉及任何现实金钱交易。

多币种模拟交易插件：**交易所风格 Web 交易端** + **1-125 倍杠杆永续合约（逐仓保证金 + 强平）** + 配置页可控走势。
与 `astrbot_plugin_zirunbi` 完全独立（独立目录、独立 `market_pro.db`、默认端口 8666 不冲突），可同时安装。

---

## 一、安装

1. WebUI「插件管理 → 安装插件」上传本 zip，或把 `astrbot_plugin_market_pro` 放进 `AstrBot/core/data/plugins/`。
2. 重启 AstrBot。
3. WebUI「插件管理 → 配置」调整走势、币种、管理员、Web 端口，保存即生效（1 秒内热加载）。

依赖（requirements.txt）：`sqlalchemy` `mplfinance` `pandas` `matplotlib` `fastapi` `uvicorn` `passlib`
绘图库缺失时 `/mkt kline` 自动降级为文字；Web 端需要 fastapi + uvicorn + passlib。

---

## 二、Web 交易端

启动后日志会打印 `Web 交易端已启动: http://127.0.0.1:8666`。

- 群里先发 `/mkt register 你的密码`（至少 6 位）开通账号（账号 = 你的 QQ / 群号）
- 浏览器打开 `http://服务器IP:8666`，用账号 + 密码登录
- `/mkt web` 随时查询地址；`web_public_url` 可配置成域名或公网地址

界面布局（对齐币安 / OKX 的合约页）：

| 区域 | 内容 |
|---|---|
| 登录页 | **免责声明（必须滚到底才能勾选同意，勾选后登录按钮才可用）** + 账号密码 |
| 顶栏 | 走势模式、开市状态、服务器时间、可用余额、当前账号 |
| 左栏 | 行情列表（币种 / 最新价 / 今日涨跌），可搜索、点击切换 |
| 中栏 | Canvas K 线（30/60/120/240 根），蜡烛 + 最新价虚线 + 十字光标信息 |
| 右1 | 订单簿（12 档买卖 + 累计深度条）+ 最新成交 |
| 右2 | 下单面板：做多/做空、**杠杆滑条 1-125x**、保证金、仓位价值/数量/强平价/手续费实时预览 |
| 底部 | 持仓（半平/平仓）、**历史成交（合约平仓 + 现货买卖混合）**、现货持仓、账户汇总 + 一键全平 |

### 历史成交

「历史成交」标签里会同时显示两类记录，按时间倒序：

- **合约**：方向（多/空/强平）、杠杆、数量（实际平仓量）、开仓价、平仓价、盈亏、状态（已平仓/已强平）
- **现货**：方向（买入/卖出）、数量、成交价、状态（已成交）

### 免责声明

登录页有一段可滚动的免责声明，**必须滚动到底部才会解锁「我已阅读并同意」勾选框，勾选后登录按钮才可点**。
第一次同意后会记住（`localStorage`），之后登录只需勾选即可。声明内容明确指出：纯模拟环境、不接入支付、
不可充提兑换、价格由本机算法生成、数据可被重置、请勿使用敏感信息作为账号密码。

---

## 二·五、管理控制台（插件页面）

插件自带一个 AstrBot 面板页面，放在 `pages/console/`，安装后会自动出现在「插件管理」里该插件的
**页面 / 控制台**入口（AstrBot 的插件页机制，`pages/<名字>/index.html` 自动发现）。

### 一键重置股价的入口

| 入口 | 二次确认方式 |
|---|---|
| **管理控制台页面**（推荐） | 弹窗 + 手动输入 `RESET` |
| 聊天 | `/mkt resetprice`（管理员，命令不在帮助里列出） |
| 配置页（备用） | 勾选「⚠ 一键重置股价（备用）」保存，执行完**自动改回未勾选**，60 秒内重复勾选会被忽略 |

页面内容：

| 区块 | 说明 |
|---|---|
| 概览 | 币种数量、市场状态、接口连通（显示「面板同源」或「端口直连」） |
| 股价一览 | 每个币种的当前价 / 重置目标（= 配置页价格）/ 偏差百分比 |
| 危险操作 | **「一键重置全部股价」按钮 → 弹出二次确认弹窗**（列出所有币种 当前→目标，必须手动输入 `RESET` 才能点确认） |

重置会做：所有币种价格 → 配置页填的价格；当前 K 线清空重来；今日涨跌基准归零。
**余额 / 持仓 / 历史成交 / 合约仓位不受影响**，但价格突变会立刻改变持仓市值与合约盈亏，
高倍杠杆仓位可能瞬间触发强平。接口端还做了兜底：请求体必须带 `confirm=RESET`，否则 400；
配置了 `admin_api_key` 时还必须带匹配的 `X-Admin-Key`。

### 管理页读不到数据怎么办

AstrBot 的插件页是跑在 **`sandbox` iframe** 里的（没有 `allow-same-origin`），这意味着：

- 页面里 **不能用 `localStorage`**（会直接抛异常）
- 页面里的 `fetch` **带不上面板的登录 Cookie**

所以页面**通过 AstrBot 注入的 postMessage 桥**（`window.AstrBotPluginPage.apiGet / apiPost`）调用插件 API，
桥由面板代发请求并带上登录态。插件注册了 `astrbot_plugin_market_pro/overview`、`market_pro/overview`
等多个前缀，不同 AstrBot 版本用哪个插件名都能命中。

每一步都有超时（桥 8 秒 / 同源 5 秒 / 直连 4 秒），不会再卡在「读取中」。读取失败时页面会直接显示原因，
并提供「备用：直连插件端口」输入框（默认填好 `http://当前主机:8666`），点「尝试直连」才会去试。

排查顺序：

1. 日志里有没有这一行（有 = 插件 API 注册成功）：
   ```
   [market_pro] 已注册插件 Web API 前缀: astrbot_plugin_market_pro, market_pro
   ```
2. 确认装的是 **v1.6.0 及以上**。
3. 页面点「重新读取」；仍失败就用备用直连框填 `http://服务器IP:8666` 点「尝试直连」。
4. 再不行：群里发 `/mkt resetprice`，或配置页勾选备用开关。

> v1.5.0 起插件自带的 `http://IP:端口/console` 页面已移除，管理控制台只在 AstrBot 面板里提供。

K 线颜色可在配置页选「国际（绿涨红跌）」或「中国（红涨绿跌）」。

> 端口被占用时日志会提示，改 `web_port` 即可；`web_host` 默认 `0.0.0.0`（局域网可访问），只想本机用就填 `127.0.0.1`。

---

## 三、合约与杠杆（1 - 125 倍，逐仓）

### 规则

| 项目 | 公式 / 默认值 |
|---|---|
| 名义价值 | `notional = 保证金 × 杠杆` |
| 数量 | `qty = notional / 开仓价` |
| 多单强平价 | `entry × (1 + 维持率 − 1/杠杆)` |
| 空单强平价 | `entry × (1 − 维持率 + 1/杠杆)` |
| 未实现盈亏 | `(标记价 − 开仓价) × qty × (多 +1 / 空 −1)` |
| ROE | `未实现盈亏 / 保证金` |
| 保证金率 | `(保证金 + 未实现盈亏) / 名义价值`，跌破 `maintenance_margin_rate` 即强平 |
| 手续费 | 开仓、平仓各收 `notional × contract_fee_rate`（默认 0.05%） |
| 强平结果 | 逐仓：亏掉该仓位全部保证金，不追加负债，不影响其他仓位 |

举例（默认参数，维持率 0.5%）：

- 100 保证金 × 20x → 名义 2000，100 元开仓可买 20 枚；强平价 `100 × (1+0.005−0.05) = 95.5`
- 50 保证金 × 125x 做空 → 名义 6250，强平价 `100 × (1−0.005+0.008) = 100.3`（涨 0.3% 就爆）

> 杠杆超过 125 会被夹到 125，小于 1 夹到 1。

### QQ 命令

```
/mkt long  <币> <保证金> [杠杆]     开多（杠杆默认配置 default_leverage=20）
/mkt short <币> <保证金> [杠杆]     开空
/mkt pos                            我的仓位（含强平价、ROE、保证金率）
/mkt close <仓位ID> [比例]           平仓（比例 0.5 = 半平，省略 = 全平）
/mkt closeall                       一键全平
/mkt lev                            杠杆规则说明
```

---

## 四、配置页

保存后 **1 秒内热生效**，不用重启。

### 走势模式 `market_mode`（全局默认）

| 模式 | 行为 | 关键参数 |
|---|---|---|
| 上涨 | 每 tick 稳定上涨 | `rise_strength` |
| 下跌 | 每 tick 稳定下跌 | `fall_strength` |
| 横盘震荡 | 围绕基准价正弦摆动 | `sideways_amplitude`（振幅）、`sideways_period`（周期） |
| 连续上涨 | 连拉 N 个 tick 后切 `after_trend_mode` | `rally_ticks`、`rally_step` |
| 连续下跌 | 连砸 N 个 tick 后切 `after_trend_mode` | `dump_ticks`、`dump_step` |
| 自由随机 | 高斯随机游走 | `random_volatility` |
| 目标涨跌 | 自动推向今日目标涨跌幅 | `daily_target_pct`、`target_window_minutes` |

### 单币种独立走势（v1.2 新增）

**每个币种可以有自己的走势**，不再一起同涨同跌。两种设置方式：

1. **配置页**：币种列表里每个币种都有「该币种单独走势」下拉框，留空 = 跟随全局。
2. **命令**：
   ```
   /mkt mode                     查看每个币种当前走势和来源
   /mkt mode 下跌 ZRB             只把 ZRB 改成下跌（持久化，写回配置页）
   /mkt mode 连续上涨 MOON         只让 MOON 连涨
   /mkt mode 上涨                 全局改（所有没单独设置的币种跟随）
   /mkt mode auto ZRB             清除 ZRB 的单独设置，回到全局
   ```

优先级：`单币种命令覆盖` > `单币种配置页设置` > `全局命令覆盖` > `全局配置`。

> **Web 端默认不显示走势**（配置项 `web_show_mode`，默认 `false`）。关闭时网页上不会出现
> 「走势 上涨」这类字样，`/api/symbols`、`/api/config` **也不返回模式字段**，即使有人开 F12 看
> 网络请求也看不出价格是人为设的。只有你自己调试时才需要打开它。走势信息仍然可以在群里用
> `/mkt mode` 查看。

调参示例：

- 全场每天稳涨 5%：`market_mode=目标涨跌`，`daily_target_pct=5`，`target_window_minutes=30`
- 阴跌磨人：`market_mode=下跌`，`fall_strength=0.004`，`tick_interval=30`
- 暴涨暴跌气氛：`market_mode=横盘震荡`，`sideways_amplitude=0.12`，`sideways_period=8`，`tick_interval=3`
- 先拉一波再回落：`market_mode=连续上涨`，`rally_ticks=20`，`rally_step=0.015`，`after_trend_mode=横盘震荡`

其他：`noise` 抖动、`tick_interval` 一根 K 线的时长、`max_tick_change` 单 tick 熔断、`trade_impact` 买卖冲击价格、`enforce_trading_hours`（默认关，全天可交易）。

### 币种列表 `coins`（一键添加）

配置页点「添加条目」→ 选「币种」→ 填 `symbol`（自动大写）/ `name` / `price` / `desc`（`/mkt info` 显示）/ `mode`（单独走势）/ `enabled`。

也可以 `/mkt add MOON 33.5 月亮币测试描述`，会**同时写回配置页**。

#### 价格以谁为准（`config_price_priority`）

| 设置 | 行为 |
|---|---|
| ✅ 开（默认） | **重启后价格 = 配置页里填的价格**。新加的币、或你在配置页改过价格的币，启动/热加载后都会重置到该价格，K 线从该价重新开始，今日涨跌也从 0 算起 |
| ⬜ 关 | 接续数据库里上次的收盘价（价格连续，不会因重启跳变） |

注意：开着这个选项时，**只有在配置页里真正改动了某个币的价格**才会触发重置，改别的设置（振幅、走势、管理员等）不会影响价格。

### 合约参数

`max_leverage`（默认 125）、`default_leverage`（20）、`max_positions_per_user`（5）、`contract_fee_rate`（0.0005）、`maintenance_margin_rate`（0.005）、`auto_close_on_liq`、`chart_color_style`。

### Web 参数

`web_enabled`、`web_host`、`web_port`（8666）、`web_public_url`。

---

## 五、全部命令

### 用户

```
/mkt price [币]        实时价格
/mkt change            今日涨跌
/mkt kline <币>        K 线图（图片）
/mkt info [币]         币种资料
/mkt coins             币种列表
/mkt time              市场状态 / 走势 / 更新间隔
/mkt buy  <币> <数量> [价格]   现货买入（限价/市价）
/mkt sell <币> <数量> [价格]   现货卖出
/mkt orders            我的现货挂单
/mkt cancel <ID>       撤单
/mkt assets            我的现货资产
/mkt rank              资产排行
/mkt register <密码>    开通 Web 账号
/mkt web               Web 地址
/mkt lev               杠杆规则
/mkt long|short|pos|close|closeall   合约相关
```

### 管理（需 `admin_ids`）

```
/mkt give <用户> <金额>                  加钱
/mkt take <用户> <金额>                  扣钱
/mkt pay <用户> <金额>                   自己 → 对方 转账
/mkt transfer <源> <目标> <金额>          任意账户互转
/mkt grant <用户> <币> <数量>             加股（按当前价记成本）
/mkt revoke <用户> <币> <数量>            收股
/mkt sendstock <源> <目标> <币> <数量>     账户间转股（成本结转）
/mkt add <币> <价格> [描述]               加币（同步配置页）
/mkt del <币>                            删币
/mkt setprice <币> <价格>                 立刻设定价格
/mkt mode <模式>  /  /mkt mode auto       切换 / 恢复走势
/mkt mopen | /mkt mclose                 强制开市 / 休市
/mkt reset <用户>                        重置账户
/mkt log                                 最近管理操作
```

---

## 六、数据

- 数据库：`插件目录/market_pro.db`（SQLite）
- 配置：`AstrBot/core/data/config/astrbot_plugin_market_pro_config.json`
- 表：`users` `holdings` `coins` `market_history` `orders` `positions` `admin_logs`

---

## 七、常见问题

**Web 打不开？** 看日志有没有「端口被占用」，换 `web_port`；确认防火墙放行；`web_host` 为 `0.0.0.0`。

**登录提示未设置密码？** 在群里发 `/mkt register 你的密码`。

**刚开仓就被平？** 杠杆过高。125x 时价格朝反方向动 0.8% 就会强平，`/mkt lev` 有说明。

**K 线图没有柱子？** 市场刚启动历史还少，等几个 tick 或调小 `tick_interval`。

**中文显示方块？** 配置页 `font_path` 填 `C:\Windows\Fonts\msyh.ttc`。

**「连续上涨」为什么涨到 30% 就变横盘了？**
这是设计如此，不是 bug。`连续上涨` 是**有限次**拉升：跑满 `rally_ticks` 个 tick 后自动切换到
`after_trend_mode`（默认「横盘震荡」）。默认参数下：

```
总涨幅 ≈ (1 + rally_step) ^ rally_ticks − 1
      = (1 + 0.02) ^ 15 − 1 = +34.6%      ← 你看到的「30% 左右」
```

**想让它一直涨下去**，两种改法（任选其一）：

| 目标 | 改法 |
|---|---|
| 无限连涨，永不切换 | `连续上涨：持续 tick 数` 填 **0** |
| 涨完继续以较缓速度涨 | `连续上涨/下跌结束后切换到` 选 **上涨** |
| 干脆不要「停」这个概念 | 直接用 **上涨** 模式（每 tick 固定涨 `rise_strength`，永不自动停） |

`/mkt time` 和 `/mkt mode` 现在会直接打印当前参数换算后的结果，例如
`连涨 15 个 tick × 2.00% ≈ 累计 +34.6%，跑完后转「横盘震荡」`，不用再自己算。

---


- 新增 1-125 倍逐仓杠杆合约：保证金、强平、手续费、ROE、一键全平。
- 新增 `/mkt register`、`/mkt web`、`/mkt long|short|pos|close|closeall|lev`。

### v1.0.0
- 首个版本：配置页可控走势、一键添加币种、管理员加钱/转账/转股。
