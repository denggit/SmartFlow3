# Smart Money 钱包分析工具

## 概述

这是一套用于分析 Solana 链上 Smart Money 钱包的工具集，通过分析钱包的历史交易数据，评估钱包的投资能力和策略类型。

## 工具列表

### 1. analyze_wallet.py - 单钱包分析工具

分析单个钱包的交易历史，生成详细的投资画像报告。

#### 使用方法

```bash
cd tools/smart_money
python analyze_wallet.py <钱包地址>
```

#### 功能特性

- ✅ 智能解析 SWAP 交易
- ✅ 按代币数量比例分配成本/收益（修复了平均分配的 Bug）
- ✅ 正确处理 SOL/WSOL 重复计算问题
- ✅ 实时价格查询（DexScreener API）
- ✅ 多维度评分系统（稳健中军、土狗猎手、钻石之手）
- ✅ 详细的错误处理和日志记录

#### 输出示例

```
🧬 战力报告 (V5): ABC123...
════════════════════════════════════════════════════════════
📊 核心汇总:
   • 项目胜率: 65.0% (基于 20 个代币)
   • 累计利润: +125.50 SOL
   • 持仓中位: 45.0 分钟
------------------------------
🎯 战力雷达 (置信度: 高):
   🛡️ 稳健中军: ████████░░ 80分
   ⚔️ 土狗猎手: ██████████ 100分
   💎 钻石之手: ██████░░░░ 60分
------------------------------
🏆 综合评级: [S级] 100 分
📝 状态评价: 盈亏比: 3.25 | 代币数: 20
```

### 2. batch_analyze.py - 批量分析工具

批量分析多个钱包地址，自动过滤低质量钱包，生成 Excel 报告。

#### 使用方法

1. 准备钱包列表文件 `tools/wallets.txt`（每行一个地址）
2. 运行批量分析：

```bash
cd tools/smart_money
python batch_analyze.py
```

#### 功能特性

- ✅ 批量并发分析（可配置并发数）
- ✅ 自动黑名单过滤（低质量钱包自动加入 `wallets_trash.txt`）
- ✅ Excel 报告导出（包含所有关键指标）
- ✅ 进度条显示
- ✅ 详细的日志记录

#### 输出文件

- `tools/results/wallet_ranking_v5_YYYYMMDD_HHMMSS.xlsx` - Excel 分析报告
- `tools/wallets_trash.txt` - 自动生成的黑名单

## 核心优化（V5 版本）

### 1. 修复代币归因逻辑

**问题**：原版本使用平均分配，导致成本/收益计算不准确。

**解决方案**：按代币数量比例分配

```python
# 旧版本（错误）
avg_cost = abs(sol_change) / len(buys)  # 平均分配

# 新版本（正确）
total_tokens = sum(buys.values())
cost_per_token = abs(sol_change) / total_tokens
for mint, token_amount in buys.items():
    attribution[mint] = cost_per_token * token_amount  # 按比例分配
```

### 2. 改进错误处理

- 使用具体的异常类型（`aiohttp.ClientError`, `asyncio.TimeoutError`）
- 添加详细的日志记录
- 实现重试机制（价格查询）

### 3. 增强价格查询健壮性

- 实现重试机制（最多 3 次）
- 处理 Rate Limit（429 错误）
- 超时控制（10 秒）
- 价格缺失时的降级策略

### 4. 代码结构优化

- 使用类封装，职责分离
  - `TransactionParser`: 交易解析
  - `TokenAttributionCalculator`: 代币归因计算
  - `PriceFetcher`: 价格获取
  - `WalletAnalyzer`: 核心分析引擎
- 模块解耦，易于测试和维护

## 配置要求

### 环境变量

需要在项目根目录的 `.env` 文件中配置：

```env
HELIUS_API_KEY=your_helius_api_key
```

### Python 依赖

```bash
pip install aiohttp pandas openpyxl tqdm python-dotenv
```

## 文件结构

```
tools/
├── smart_money/
│   ├── __init__.py
│   ├── analyze_wallet.py      # 单钱包分析
│   ├── batch_analyze.py        # 批量分析
│   └── README.md               # 本文档
├── wallets.txt                 # 钱包地址列表
├── wallets_trash.txt           # 黑名单（自动生成）
└── results/                    # 分析结果目录
    └── wallet_ranking_v5_*.xlsx
```

## 注意事项

1. **API 限制**：注意 Helius 和 DexScreener 的 API 速率限制
2. **数据准确性**：价格数据依赖 DexScreener，可能存在延迟或缺失
3. **黑名单机制**：低质量钱包会自动加入黑名单，避免重复分析
4. **并发控制**：默认并发数为 2，可根据 API 限制调整

## 设计模式

- **单一职责原则**：每个类只负责一个功能
- **依赖注入**：通过构造函数注入依赖
- **策略模式**：不同的归因策略可扩展
- **工厂模式**：组件创建和初始化

## 版本历史

- **V5** (2026-02-01): 优化版
  - 修复代币归因逻辑
  - 改进错误处理
  - 增强价格查询健壮性
  - 代码结构重构

- **V4 Pro**: 修复 SOL 重复计算与多代币归因 Bug
