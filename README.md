# 位域消息目录服务与总线观测仓

Python 3.11，纯标准库。两个实质源码模块：

| 模块 | 职责 |
| --- | --- |
| `bitcatalog/catalog.py` | 目录定义模型、校验（同时活动 + 物理位重叠证明）、位域编译、payload 编码/解码、可发布目录产品的序列化与加载 |
| `bitcatalog/warehouse.py` | SQLite 观测仓：把每帧绑定到不可变目录产品、整帧拒绝与错误定位、保存原始字节与活动信号、半开窗口精确有理数聚合 |
| `bitcatalog/reference.py` | 独立逐位参考模型（与编译掩码码完全不同的实现），仅用于测试/核对 |
| `bitcatalog/counting.py` | 分字计件：片段拼装、交割身份/提交标志、纪元定序、读数界、换班工资清分、按整数工分分整数分钱、旧工资单重开与待重算索引 |

## 协议语义（本题合同）

* 消息：`bus`、`frame_id`、固定 `payload_bytes`（1..64）、信号表，可选一个无符号无缩放 `selector`。
* 信号：唯一 `name`、非负 `start`、`width`（1..32）、`order`、`signed`、非零有理数 `factor` 与有理数 `offset`。
* **little**：`start` 为全局位号；全局位 `k` = 第 `k//8` 字节的低位 `k%8`；连续物理位依次是值的低→高位。
* **big**：`start=(byte_index, bit_index)`，`bit_index` 从该字节最高位 0 数到最低位 7；越过字节末尾后从下一字节最高位继续；连续物理位依次是值的高→低位。
* 超出正文的定义（任一字节/位越界、`width>32`、正文长度不符）整条拒绝。
* 有符号量按 `width` 位二进制补码解释；物理值 = `raw * factor + offset`，全程 `fractions.Fraction` 精确计算（JSON 中的十进制文本/浮点按十进制字面量解析，不走二进制浮点）。
* 普通信号始终活动；分支信号带一个或多个 selector 整数，仅当本帧 selector 命中时存在。
* 发布时拒绝任何**可能同时活动**且物理位重叠的信号对，包含跨字节与 little/big 两种位序混合；互斥分支（分支集合不相交）可复用同一批位。selector 自身不得与任何活动位重叠。
* 编码入口：给定活动信号 raw 值与 selector，生成真实 payload；未分配位恒为 0；写完立即用编译提取路径回读校验。

## 目录产品

`build_product()` 产生产品指纹（对规范化定义 JSON 的 SHA-256）与编译对象；`CatalogProduct.save()` 写单个 JSON 文件（含嵌入定义、编译摘要、哈希）。`load_product()` 加载时重算哈希，内容被改动即拒绝（`bad_product_hash`）。同一份原始 hex 用 v1 与 v2 两个产品分别解释，各自的结果互不干扰、可长期复查。

## 观测仓

* 输入 `frame_event_id`、`bus`、`frame_id`、发生时刻（整数纳秒或 ISO-8601，保留 9 位小数）、真实 hex 正文；每批绑定一个产品。
* 错误长度 / 未知消息 / 错误编码 → **整帧拒绝**，保留错误码与位置（hex 里为 nibble 偏移；长度错误为期望字节数）；被拒绝帧同样保存原字节（若可解析）与错误信息，但不产生任何观测行。
* 成功帧保存原始字节、selector、以及**只有本帧活动的信号**。不活动信号不写行——既不是 NULL 也不是 0，因此不可能进入均值分母。
* 查询 `query_window(product, signal, start_ns, end_ns)`：半开窗口 `[start, end)`，返回次数、精确和、精确均值，以及每一条源帧身份（事件 ID、bus、frame_id、时间、原始 hex、selector）。
* 存储布局：文本 ID 只存一次（`products`、`signals` 表），400 万行的观测表只存整数，主键 `(product_id, signal_id, occurred_at_ns, frame_pk)` 为 `WITHOUT ROWID`；`signals` 表按 `(product, bus, frame_id, name)` 建立信号身份，同名但属于不同消息的信号不会在聚合中混在一起（查询时若仍有歧义会要求显式 `bus`/`frame_id`）；数值以精确分子/分母对存储（超出 int64 时退化为十进制文本）。
* `batch_size` 只影响事务/内存粒度，不影响结果（测试与核对脚本都验证这一点）。

## 分字计件与换班工资清分

`bitcatalog/counting.py` 在观测仓之上实现工资房所需的计件单。所有输入均来自本地生成并已入库的帧，不依赖任何外部数据。

### 目录扩展：计数产品

`build_product(messages, counters)`（`catalog_v1` 之外可另建计数产品）接受 `CounterDef`。一个计数产品声明：

* `low` / `high` 两段：各段所在 `bus/frame_id/signal`、**有效位宽**（`width`，可小于字段物理宽度）、**提交标志**字段 `commit`、**交割身份**字段 `settlement`，以及可选的**唯一递增交割序号** `sequence` 与织机身份 `loom`。
* `loom_constant`（织机固定时）或两段各自的 `loom` 字段（二选一）。
* 段值 = `(high << low.width) | low`，满量程 = `1 << (low.width+high.width)`。

身份字段必须与所属段位于同一消息，否则 `counter_identity_frame` 拒绝；段或身份字段为分支信号、宽度越界、字段不存在均拒绝。计数字段进入产品规范化 JSON 并参与指纹，`save`/`load_product` 会重算哈希并重新编译计数器。

### 拼装（`assemble_counter` / `assemble_from_warehouse`）

* 片段按 `(织机, 交割身份, 产品, 计数器)` 归并；**到件顺序任意**，晚到的片段直接填入对应槽位。
* **只有提交标志置位**的片段可成为已确认计数；未提交的试读永远不能变成计数，也不在其他分支/物理位上凑数。
* 同一段重复且同值 → 去重（记录为 `duplicate`）；同段重复且异值 → `conflict`，该读数不可用。
* 只到一段（另一段缺失或未提交）→ `incomplete`（缺件），记录缺失段名。

### 纪元与读数

* 已确认读数按设备给出的**唯一递增交割序号**定序（不看到达时间），**同刻**（相同纳秒）的交班点与班末也据此稳定排序。
* 首条读数开启纪元；读数回退、或与上一条间隔达到满量程，则另开纪元，旧纪元标记 `split_required`（跨纪元区间待拆单，不能直接支付）。
* **交叉半份**：当某界只到一段时，可显式借下一读数的另一段作诊断（`crossed_with` / `naive_value`），但交叉读数**绝不是已确认界**，不能用于工资结算。

### 换班工资（`settle_day`）

* 交班必须引用一次**完整的已确认读数**作界；两组分开结算，界读不全则暂记当日增量、**不猜两组份额**（`held`）。
* 组内按签定的整数工分比例分整数分钱：先取每人的精确份额下取整，再把余额逐分给小数余数最大者，同余按工号（`split_integer`，最大余数法）。
* **例**：班始 `0x01FE`、交班 `0x01FF`、班末 `0x0200`，低段宽 8，每件 1 分。交班高段未到时，错误拼入班末高段会算成 `0x02FF`；正确结果是交班保持缺件。完整后两班各 1 件；晚班甲乙工分相等、甲号小，甲得 1 分；把甲乙工分改为 1 和 2，则乙得 1 分。

### 旧工资单重开与待重算（`PayrollLedger`）

* 每张工资单记录所用各界的 pin（读数身份 SHA-256）；索引 `pin -> (日容器, 班组, 班次)`。
* 夜间常有大量只到半份的读数、许多工资单同时引用少数交班点。归档后补证（新提交片段到齐）改变某些读数时，`changed_pins` 找出变化，`affected_containers` 只重开引用过这些读数的日容器，`recalculate` 重出工资单并列出**待重算的班组**（`groups_to_recompute`）与新旧金额。

### 演示与产出

```bash
python3.11 tools/payroll_demo.py --out artifacts
```

`artifacts/payroll_frames.jsonl`（含 12 条只到半份的夜间读数、异序补件 200/201、同刻 202/203、归档后补证的 204）、`payroll_late_frames.jsonl`、`payroll_catalog.json`、`payroll_report.json` 覆盖：异序补件、同刻交班、归档后补证、大量半份读数、50 张工资单共引少数交班点（首轮全部 `held`，补证后一次性重算出 `groups_to_recompute`）。

## 复跑

```bash
# 1) 单元/属性测试（56 项）
python3.11 -m unittest discover -s tests -v

# 2) 生成完整交付物：200 消息 × 50 信号、100000 帧、v1/v2 两个目录产品、数据仓
python3.11 tools/build_artifacts.py

# 3) 独立核对：逐位参考模型 × 编译码、抽取帧比对、批大小不变性、重开重建
python3.11 tools/verify.py

# 4) 演示
python3.11 tools/demo.py

# 5) 分字计件/换班工资清分演示（生成 payroll_* 交付物）
python3.11 tools/payroll_demo.py
```

快速小规模冒烟：

```bash
python3.11 tools/build_artifacts.py --out /tmp/art_small --messages 12 --frames 3000
python3.11 tools/verify.py --out /tmp/art_small --sample 200 --batch-sample 200
```

## 交付物（`artifacts/`）

| 文件 | 内容 |
| --- | --- |
| `catalog_v1.json` / `catalog_v2.json` | 可发布目录产品（嵌入定义 + 编译摘要 + 内容哈希） |
| `catalog_v1.definitions.json` / `_v2` | 源定义（修订版对前 40 条消息重命名并改变缩放） |
| `frames_v1.jsonl` | 100000 条总线事件（含非法 hex / 错误长度 / 未知消息） |
| `observations_v1.db` | 实际数据仓（绑定 v1 与 v2 两个产品的观测） |
| `build_report.json` | 规模、计时、拒绝样本、样例聚合、修订对照、重开重建核对 |
| `verification_report.json` | 独立核对结果 |
| `payroll_frames.jsonl` / `payroll_late_frames.jsonl` | 分字计件场景的本地帧（含半份、异序、同刻、归档补证） |
| `payroll_catalog.json` | 含计数产品声明的可发布目录产品 |
| `payroll_report.json` | 异序补件、同刻交班、归档补证、大量半份与批量重算的演示结果 |

## 规模与性能

200 消息 × 50 信号（10000 个信号）、100000 帧、约 400 万条活动观测。

* 目录构建（定义解析 + 重叠证明 + 掩码编译）：约 1 秒（全量 200 条）。
* 编码/解码走预编译掩码 + 整数提取；同 width 重复 raw 模式命中值缓存，避免逐帧构造 `Fraction`。
* 观测仓批量插入走单事务 + `executemany`，400 万行需要十余秒；查询按 `(product, signal, 时间)` 主键范围扫描。

以上数字以 `build_report.json` 为准（每次运行机器不同，数字会浮动）。

## 测试与核对覆盖

* 跨字节两种位序的位位置与单比特权重（逐位参考模型对照，含 1..12 / 17..32 宽度）。
* 有符号最小值（负的最小值、-1、最大值）在跨字节位置下的补码解释与精确值。
* 不同互斥分支复用相同位；分支集合有交集时重叠仍被拒绝；selector 重叠被拒绝；跨位序重叠被拒绝。
* 有理数缩放（`1/1000`、`-5/7`、十进制文本 `0.1`）与 `raw*factor+offset` 的精确性。
* 非法 hex、奇数 nibble、错误长度、未知消息整帧拒绝并保留位置。
* 半开窗口边界；不活动信号不进入分母；批大小 1/7/64/5000 结果逐帧一致。
* 目录修订后同一原件可解释为不同信号；旧产品结果仍可复查；重开后从原始 hex 重建抽样结果一致。
* 分字计件：异序补件仍拼齐；只到半份记缺件不臆造；未提交试读不确认；同段同值去重、异值冲突；同刻读数按交割序号定序。
* 纪元：读数回退或跨满量程另开纪元并标记待拆单，跨纪元区间不直接支付。
* 换班界：交班引用完整已确认读数；界读不全暂记日增量、不猜两组份额；错误借下一条高段会得到 `0x02FF` 的诊断值而真实值为 `0x01FF`。
* 工分分账：整数分钱按工分先取整再按最大余数逐分、同余按工号分配（随机属性校验和守恒与每人误差 ≤1 分）。
* 重开：大量半份读数、许多工资单共引少数交班点时，仅重开引用变化读数的日容器，并列出待重算班组与新旧金额。
