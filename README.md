# StockWatcher 私人盯盘助理

面向单人使用的 AstrBot 插件。首先支持美股，免费数据优先、接受延迟；后续加入 AI 决策辅助、条件监控和多智能体研究。

当前 `v0.1.0` 实现路线图第 01～03 步的代码：插件入口、手动行情查询、持仓与关注池、成交及现金账本。真实 AstrBot 加载/平台送达和 Alpaca 权限仍需在目标环境验证，详见 [验证记录](docs/VALIDATION.md)。本版尚不生成投资建议，也不自动定时盯盘或提交订单。

## 安装

目标环境：Python 3.12+、AstrBot `>=4.28.2,<5`。代码参考本地 `4.29.0-beta.2` 的接口；尚未把该版本范围全部做运行验证。

1. 将本仓库置于 AstrBot 的 `data/plugins/astrbot_plugin_stockwatcher/`，或使用 AstrBot 支持的插件安装方式。
2. 让 AstrBot 安装 `requirements.txt` 中的依赖；也可在其 Python 环境执行 `pip install -r requirements.txt`。
3. 重载插件，发送 `/盯盘 帮助`、`/盯盘 状态`。也支持 `/stockwatch` 别名。
4. 行情需要 Alpaca 凭证；账本无需行情或模型配置即可使用。

插件数据存于宿主的 `data/plugin_data/astrbot_plugin_stockwatcher/stockwatcher.sqlite3`，不会写入插件源代码目录。使用个人可控的 AstrBot 入口，访问控制沿用宿主设置。

## 配置与行情验证

在插件配置页填写 `alpaca_key_id`、`alpaca_secret_key`，或在 AstrBot 进程环境中设置 `APCA_API_KEY_ID`、`APCA_API_SECRET_KEY`。不要把凭证发给聊天机器人。配置页的遮罩只影响显示，不加密配置文件。

`alpaca_account_mode` 选择凭证所属的 `paper` 或 `live`；仅决定只读交易日历和标的资料的接口，不调用下单接口。展示时区默认 `Asia/Shanghai`，市场时段使用 `America/New_York` 自动处理夏令时与提前收市。

行情使用显式 `feed=sip` 的历史接口，免费查询默认截止当前时间之前 16 分钟。不是免费实时 SIP 推送。分钟数据只保留完整的常规时段分钟；供应商日线标记为 `provider_daily`，其是否包含盘前盘后尚需实测，不假称纯常规日线。日线剔除尚未结束的交易日。

可用命令：

```text
/盯盘 行情 MU
/盯盘 行情 MU 分钟
/盯盘 行情 MU 日线 all
/盯盘 日历 2026-11-27
/盯盘 诊断 MU
/盯盘 缓存 MU
```

`raw` 为原始价，`split`/`dividend`/`all` 为供应商的相应调整口径。不同口径单独存储，每次保存完整查询快照，不把不同日期生成的调整历史直接拼接。行情成交量以股计，价格以 USD 计。

`缓存` 显示原抓取时间并明确未联网刷新；请求失败不会悄悄将旧缓存当作新行情。手动查询日线约覆盖 550 个自然日，分钟约覆盖最近 7 个自然日；没有数据时不推断停牌原因。

不依赖 AstrBot 的只读诊断入口：

```bash
uv --cache-dir /tmp/stockwatcher-uv-cache venv .venv
uv pip install --python .venv/bin/python --cache-dir /tmp/stockwatcher-uv-cache -r requirements.txt
read -r -p "Alpaca Key ID: " APCA_API_KEY_ID
read -r -s -p "Alpaca Secret Key: " APCA_API_SECRET_KEY
export APCA_API_KEY_ID APCA_API_SECRET_KEY
.venv/bin/python -m stockwatcher.probe --symbol MU
unset APCA_API_KEY_ID APCA_API_SECRET_KEY
```

模拟凭证默认 `paper`，真实账户凭证可设置 `STOCKWATCHER_ACCOUNT_MODE=live`。诊断只访问标的资料、日历与历史行情，不访问持仓/订单，不输出凭证。它使用临时数据库，退出后删除；插件内诊断会将成功查询存入插件缓存。

退出码：`0` 表示接口与字段检查得到非空结果，`1` 表示缺少足够行情，`2` 表示未配置、参数或接口错误。成功不等于已验证长期稳定性、历史完整性或所有复权事件。

## 持仓与成交账本

以下数字为操作示例，不代表真实行情。日期是纽约当地日期，可省略为纽约当天；同日事件按登记顺序计算。若需要回填同日事件的先后顺序，应先撤销受影响的后续记录，再依实际顺序重录。

```text
/盯盘 持仓 添加 MU 数量=10 成本=100 日期=2026-10-16
/盯盘 现金 设置 5000 日期=2026-10-16
/盯盘 关注 添加 MSFT 理由="等待 回调"
/盯盘 成交 买入 MU 数量=1.5 价格=100 费用=0.5 日期=2026-10-16
/盯盘 成交 卖出 MU 数量=1 价格=110 费用=0.5 日期=2026-10-16
/盯盘 持仓
/盯盘 关注
/盯盘 记录
/盯盘 撤销 4 原因=成交价录错
```

- 代码可写 `MU` 或 `US:MU`，暂不接受公司中文名或其他市场代码。
- 使用 Decimal，输入最多 12 位整数、8 位小数，不使用浮点数记账。
- 加权平均账面成本包含买入费用，卖出费用抵减收益；不是税务批次或报税成本。
- 期初持仓不反推现金，也不补造更早的交易收益。同一股票存在有效记录时，不能用“期初持仓”直接覆盖它。
- 现金初始为未知，允许登记实际交易但不会声称校验过购买力。设置余额后，重放过程中现金不能为负。
- 现金是手工登记的记账余额，不是券商已交收资金、融资购买力或可卖数量；本版不模拟交收和融资，不校验实际交易权限。
- `现金 设置` 是当时余额快照，之后的交易与资金事件从该余额计算；更早历史不覆盖之后的余额快照。建议先录入期初持仓和对应时点的现金，再录后续成交。
- 同一平台消息 ID 的重复投递不会重复入账；手工重复发送的新消息是新的登记，有新的记录 ID。
- 撤销保留原记录与原因。如果撤销会造成后续超卖或历史负现金，将拒绝整个撤销操作，需按依赖顺序更正。

其他资金与拆股事件：

```text
/盯盘 现金 入金 1000 日期=2026-10-16
/盯盘 现金 出金 100 日期=2026-10-16
/盯盘 现金 收入 10 日期=2026-10-16 备注=现金分红
/盯盘 拆股 MU 比例=2:1 日期=2026-10-16
/盯盘 关注 移除 MSFT
```

拆股改变数量、保留总成本；碎股处置和现金替代需按券商实际结果核对，不自动推断。现金收入不冒充股票卖出收益。持仓和关注状态独立保留，移除关注不删成交。

## 主动消息测试与生命周期

```text
/盯盘 提醒目标 当前
/盯盘 推送测试
/盯盘 提醒目标 清除
```

提醒目标只保存选定会话。没有保存目标时，测试向当前会话发送；测试通过不代表已开启自动监控，也不保证平台最终送达。某些平台不支持主动消息，需要在实际平台核实。

本版不创建轮询后台任务；重载/卸载会取消正在进行的查询或测试发送并关闭 HTTP 客户端与数据库。正常退出后可以重新加载并恢复账本。

## 本地测试

```bash
uv pip install --python .venv/bin/python --cache-dir /tmp/stockwatcher-uv-cache pytest ruff
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

测试使用 HTTP MockTransport 和 AstrBot API 测试替身，不调用外部行情、不发送真实平台消息。真实环境验收步骤见 [验证记录](docs/VALIDATION.md)。

[实施路线图](docs/ROADMAP.md) · [架构](docs/PLAN.md) · [AI 设计](docs/DECISION_SUPPORT.md) · [数据调研](docs/RESEARCH.md)
