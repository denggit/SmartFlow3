#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : analyze_wallet.py
@Description: 大哥筛选器 (最终版) - 增加中位数、秒男率、风险评分
"""
import asyncio
import os
import sys
from collections import defaultdict
import statistics
import aiohttp

# 导入配置中的 API Key
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import API_KEY

# === ⚙️ 配置区 ===
TARGET_TX_COUNT = 20000  # 建议拉取 1000 条以获得准确数据
MIN_SOL_THRESHOLD = 0.1  # 忽略小于 0.1 SOL 的粉尘交易


# =================

async def fetch_history_pagination(session, address, max_count=500):
    """ 自动翻页拉取交易记录 """
    all_txs = []
    last_signature = None

    print(f"🔍 正在深度审计: {address[:6]}...")
    print(f"🎯 目标样本: {max_count} 条 (正在挖掘数据...)")

    while len(all_txs) < max_count:
        batch_limit = 100
        url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
        params = {"api-key": API_KEY, "type": "SWAP", "limit": str(batch_limit)}
        if last_signature: params["before"] = last_signature

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200: break
                data = await resp.json()
                if not data: break

                all_txs.extend(data)
                last_signature = data[-1].get('signature')
                print(f"  -> 已获取 {len(all_txs)} / {max_count}...")

                if len(data) < batch_limit: break
                await asyncio.sleep(0.2)
        except Exception:
            break

    return all_txs[:max_count]


def parse_trades(transactions, target_wallet):
    """ 解析交易流 (增加金额过滤) """
    positions = defaultdict(list)
    closed_trades = []

    IGNORE_MINTS = ["So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"]

    for tx in reversed(transactions):
        if 'tokenTransfers' not in tx: continue
        timestamp = tx.get('timestamp', 0)
        sol_change, token_change, token_mint = 0, 0, ""

        for nt in tx.get('nativeTransfers', []):
            if nt['fromUserAccount'] == target_wallet: sol_change -= nt['amount'] / 1e9
            if nt['toUserAccount'] == target_wallet: sol_change += nt['amount'] / 1e9

        for tt in tx.get('tokenTransfers', []):
            if tt['mint'] in IGNORE_MINTS: continue
            token_mint = tt['mint']
            amt = tt['tokenAmount']
            if tt['fromUserAccount'] == target_wallet: token_change -= amt
            if tt['toUserAccount'] == target_wallet: token_change += amt

        if not token_mint or token_change == 0: continue

        # 过滤掉金额过小的噪音交易
        if abs(sol_change) < 0.01 and sol_change != 0: continue

        if token_change > 0 and sol_change < 0:  # BUY
            positions[token_mint].append({"time": timestamp, "cost_sol": abs(sol_change)})

        elif token_change < 0 and sol_change > 0:  # SELL
            if token_mint in positions and positions[token_mint]:
                open_pos = positions[token_mint].pop(0)

                # 再次过滤：如果买入成本太低，不计入统计
                if open_pos['cost_sol'] < MIN_SOL_THRESHOLD: continue

                hold_time = (timestamp - open_pos['time']) / 60
                profit = sol_change - open_pos['cost_sol']
                roi = profit / open_pos['cost_sol'] if open_pos['cost_sol'] > 0 else 0

                closed_trades.append({
                    "token": token_mint,
                    "hold_time": hold_time,
                    "roi": roi,
                    "profit": profit,
                    "cost": open_pos['cost_sol']
                })

    return closed_trades


def calculate_score(win_rate, median_hold, sniper_rate, profit):
    """ 🤖 AI 评分算法 """
    score = 100
    reasons = []

    # 1. 胜率惩罚
    if win_rate < 0.4:
        score -= 30; reasons.append("胜率过低")
    elif win_rate < 0.5:
        score -= 15

    # 2. 持仓时间惩罚 (核心)
    if median_hold < 5:
        score -= 40; reasons.append("典型的秒男(PVP)")
    elif median_hold < 30:
        score -= 20; reasons.append("持仓过短")

    # 3. 秒男率惩罚
    if sniper_rate > 0.3: score -= 20; reasons.append("高频刷单嫌疑")

    # 4. 盈利惩罚
    if profit < 0: score -= 20; reasons.append("总账户亏损")

    return max(0, score), ", ".join(reasons)


async def main():
    if len(sys.argv) < 2: return
    target = sys.argv[1]

    async with aiohttp.ClientSession() as session:
        txs = await fetch_history_pagination(session, target, TARGET_TX_COUNT)
        if not txs: return
        trades = parse_trades(txs, target)
        if not trades: print("⚠️ 无有效交易数据"); return

        # === 核心统计 ===
        count = len(trades)
        wins = [t for t in trades if t['roi'] > 0]
        losses = [t for t in trades if t['roi'] <= 0]
        total_profit = sum(t['profit'] for t in trades)

        # 统计分布
        hold_times = [t['hold_time'] for t in trades]
        avg_hold = statistics.mean(hold_times)
        median_hold = statistics.median(hold_times)  # 中位数

        # 秒男率 (持仓 < 2分钟的比例)
        sniper_txs = [t for t in trades if t['hold_time'] < 2]
        sniper_rate = len(sniper_txs) / count

        # 评分
        win_rate = len(wins) / count
        score, reason = calculate_score(win_rate, median_hold, sniper_rate, total_profit)

        # === 输出报告 ===
        print("\n" + "=" * 50)
        print(f"🧬 钱包深度透视报告: {target[:6]}...")
        print("=" * 50)
        print(f"📊 样本分析: {count} 笔有效交易 (已过滤 < {MIN_SOL_THRESHOLD} SOL 的粉尘单)")
        print(f"💰 净盈利: {total_profit:+.2f} SOL")
        print(f"🏆 真实胜率: {win_rate:.1%}")
        print("-" * 30)
        print(f"⏳ 持仓时间分析 (关键):")
        print(f"   • 平均值: {avg_hold:.1f} 分钟 (易受干扰)")
        print(f"   • 中位数: {median_hold:.1f} 分钟 (真实水平) 🔥")
        print(f"   • 秒男率: {sniper_rate:.1%} (持仓<2分钟的比例)")
        print("-" * 30)

        print(f"\n📢 最终判定: {score} 分")
        if score >= 80:
            print(f"✅ [强烈推荐] 真正的波段高手！ (理由: 各项指标健康)")
        elif score >= 60:
            print(f"⚠️ [谨慎跟单] 有一定风险。 (扣分项: {reason})")
        else:
            print(f"❌ [严重警告] 千万别跟！ (致命伤: {reason})")

        print("\n📝 最近 5 笔交易快照:")
        for t in trades[-5:]:
            icon = "🟢" if t['roi'] > 0 else "🔴"
            print(f" {icon} 持仓 {t['hold_time']:.1f}m | 投入 {t['cost']:.1f} | ROI {t['roi'] * 100:+.1f}%")


if __name__ == "__main__":
    asyncio.run(main())