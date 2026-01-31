#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : analyze_wallet.py
@Description: 智能钱包画像识别 (自动判断大哥类型)
"""
import asyncio
import os
import sys
import argparse
from collections import defaultdict
import statistics
import aiohttp

# 导入配置中的 API Key
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import API_KEY

# === ⚙️ 基础配置 ===
TARGET_TX_COUNT = 20000 
MIN_SOL_THRESHOLD = 0.1 

# =================

async def fetch_history_pagination(session, address, max_count=1000):
    """ 自动翻页拉取交易记录 """
    all_txs = []
    last_signature = None

    print(f"🔍 正在深度审计: {address[:6]}... (自动画像中)")
    print(f"🎯 目标样本: {max_count} 条 (挖掘数据...)")

    while len(all_txs) < max_count:
        batch_limit = 100
        url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
        params = {"api-key": API_KEY, "type": "SWAP", "limit": str(batch_limit)}
        if last_signature: params["before"] = last_signature

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200: 
                    print(f"❌ API 错误: {resp.status}")
                    break
                data = await resp.json()
                if not data: break

                all_txs.extend(data)
                last_signature = data[-1].get('signature')
                # print(f"  -> 已获取 {len(all_txs)} / {max_count}...") # 减少刷屏

                if len(data) < batch_limit: break
                await asyncio.sleep(0.1) #稍微快一点
        except Exception as e:
            print(f"❌ 网络异常: {e}")
            break

    return all_txs[:max_count]


def parse_trades(transactions, target_wallet):
    """ 解析交易流 """
    positions = defaultdict(list)
    closed_trades = []
    IGNORE_MINTS = ["So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"]

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
        if abs(sol_change) < 0.01 and sol_change != 0: continue

        if token_change > 0 and sol_change < 0:  # BUY
            positions[token_mint].append({"time": timestamp, "cost_sol": abs(sol_change)})

        elif token_change < 0 and sol_change > 0:  # SELL
            if token_mint in positions and positions[token_mint]:
                open_pos = positions[token_mint].pop(0)
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


def calculate_score_for_mode(mode, win_rate, median_hold, sniper_rate, profit, max_roi):
    """ 针对特定模式打分 """
    score = 100
    
    if mode == 'conservative': # 稳健型：看胜率、怕回撤
        if win_rate < 0.5: score -= 30
        elif win_rate < 0.6: score -= 10
        if median_hold < 10: score -= 30
        if profit < 0: score -= 50
        if sniper_rate > 0.2: score -= 20

    elif mode == 'aggressive': # 激进型：看暴击、不怕输
        if max_roi < 5.0: score -= 40
        if win_rate < 0.3: score -= 20
        if profit < 0 and max_roi < 10.0: score -= 30
        if sniper_rate > 0.5: score -= 5 # 稍微扣一点

    elif mode == 'diamond': # 钻石手：看时间
        if median_hold < 60: score -= 50
        elif median_hold < 1440: score -= 10
        if max_roi < 3.0: score -= 20
        if sniper_rate > 0.1: score -= 30

    return max(0, score)


async def main():
    parser = argparse.ArgumentParser(description="Auto Identity Analyzer")
    parser.add_argument("wallet", help="Target Wallet Address")
    args = parser.parse_args()
    target = args.wallet

    async with aiohttp.ClientSession() as session:
        txs = await fetch_history_pagination(session, target, TARGET_TX_COUNT)
        if not txs: return
        trades = parse_trades(txs, target)
        if not trades: print("⚠️ 无有效交易数据"); return

        # === 1. 基础数据计算 ===
        count = len(trades)
        wins = [t for t in trades if t['roi'] > 0]
        total_profit = sum(t['profit'] for t in trades)
        
        hold_times = [t['hold_time'] for t in trades]
        median_hold = statistics.median(hold_times) if hold_times else 0
        
        sniper_txs = [t for t in trades if t['hold_time'] < 2]
        sniper_rate = len(sniper_txs) / count
        
        win_rate = len(wins) / count
        max_roi = max([t['roi'] for t in trades]) if trades else 0

        # === 2. 三维雷达扫描 ===
        scores = {
            "🛡️ 稳健中军": calculate_score_for_mode('conservative', win_rate, median_hold, sniper_rate, total_profit, max_roi),
            "⚔️ 土狗猎手": calculate_score_for_mode('aggressive', win_rate, median_hold, sniper_rate, total_profit, max_roi),
            "💎 钻石之手": calculate_score_for_mode('diamond', win_rate, median_hold, sniper_rate, total_profit, max_roi)
        }

        # 找出最高分
        best_role, best_score = max(scores.items(), key=lambda item: item[1])

        # === 3. 最终判决 ===
        verdict = ""
        suggestion = ""
        
        if total_profit < 0 and best_score < 60:
            verdict = "🥬 纯纯的韭菜"
            suggestion = "❌ 千万别跟！这是反向指标！"
        elif best_score < 60:
            verdict = "🤔 风格不明/菜鸟"
            suggestion = "⚠️ 暂不推荐，特征不明显。"
        else:
            verdict = f"{best_role} (匹配度 {best_score}%)"
            if "稳健" in best_role:
                suggestion = "✅ 建议放入 [Bot B] (大资金、低倍止盈)"
            elif "土狗" in best_role:
                suggestion = "✅ 建议放入 [Bot A] (小资金、高倍止盈)"
            elif "钻石" in best_role:
                suggestion = "✅ 建议放入 [Bot C] (特定策略、长线)"

        # === 4. 输出可视化报告 ===
        print("\n" + "═" * 50)
        print(f"🧬 钱包身份识别报告: {target[:6]}...{target[-4:]}")
        print("═" * 50)
        
        print(f"📊 核心数据:")
        print(f"   • 总盈亏: {'+' if total_profit>0 else ''}{total_profit:.2f} SOL")
        print(f"   • 胜  率: {win_rate:.1%}")
        print(f"   • 最高单: {max_roi*100:.0f}% (最大暴击)")
        print(f"   • 持  仓: {median_hold:.1f} 分钟 (中位数)")
        
        print("-" * 30)
        print(f"🎯 身份画像 (雷达图):")
        for role, sc in scores.items():
            bar = "█" * (sc // 10) + "░" * ((100 - sc) // 10)
            print(f"   {role}: {bar} {sc}")
            
        print("-" * 30)
        print(f"📢 最终判定: {verdict}")
        print(f"💡 战术建议: {suggestion}")
        print("═" * 50)

        if count > 0:
            print("\n📝 最近 3 笔实战:")
            for t in trades[-3:]:
                icon = "🟢" if t['roi'] > 0 else "🔴"
                print(f" {icon} 持仓 {t['hold_time']:>5.1f}m | 投入 {t['cost']:>5.2f} | ROI {t['roi'] * 100:>+6.1f}%")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
