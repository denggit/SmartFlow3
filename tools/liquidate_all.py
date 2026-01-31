#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : tools/liquidate_all.py
@Description: 一键清仓工具 - 紧急卖出所有持仓并回收租金 (含盈亏统计)
@Usage      : python tools/liquidate_all.py
"""
import asyncio
import json
import os
import sys

# --- 1. 环境设置 ---
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT_DIR)

from config.settings import RPC_URL, SLIPPAGE_SELL
from services.solana.trader import SolanaTrader
from utils.logger import logger

PORTFOLIO_FILE = os.path.join(ROOT_DIR, "data", "portfolio.json")

async def main():
    print(f"\n🗑️  [一键清仓] 正在初始化...")

    if not os.path.exists(PORTFOLIO_FILE):
        logger.warning(f"⚠️ 未找到持仓文件: {PORTFOLIO_FILE}")
        return

    try:
        with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f:
            portfolio = json.load(f)
    except Exception as e:
        logger.error(f"❌ 读取持仓文件失败: {e}")
        return

    if not portfolio:
        logger.info("✅ 当前持仓记录为空，无需清仓。")
        return

    # 2. 初始化交易员
    trader = SolanaTrader(RPC_URL)
    
    # --- 🔥 新增：记录初始余额 ---
    try:
        start_bal_resp = await trader.rpc_client.get_balance(trader.payer.pubkey())
        start_balance = start_bal_resp.value / 10**9
        logger.info(f"💰 清仓前钱包余额: {start_balance:.4f} SOL")
    except Exception as e:
        logger.error(f"无法获取初始余额: {e}")
        start_balance = 0
    # ---------------------------

    logger.info(f"🔥 发现 {len(portfolio)} 个持仓代币，准备开始清仓...")
    print("-" * 50)
    
    sold_tokens = []

    try:
        # 3. 遍历持仓并卖出
        for token_mint, data in portfolio.items():
            logger.info(f"📉 正在处理: {token_mint} ...")
            
            # 查链上余额
            try:
                balance_raw = await trader.get_token_balance_raw(str(trader.payer.pubkey()), token_mint)
            except Exception as e:
                logger.error(f"  ❌ 查询余额失败: {e}")
                continue
            
            if balance_raw <= 0:
                logger.warning(f"  ⚠️ 链上余额为 0，尝试直接关闭账户回收租金...")
                # 即使没余额，也尝试关账户回血
                await trader.close_token_account(token_mint)
                sold_tokens.append(token_mint)
                continue

            # 执行卖出
            logger.info(f"  -> 发起卖出 (数量: {balance_raw})...")
            success, _ = await trader.execute_swap(
                token_mint, 
                trader.SOL_MINT, 
                balance_raw, 
                SLIPPAGE_SELL
            )

            if success:
                logger.info(f"  ✅ 卖出成功！")
                
                # 回收租金
                logger.info(f"  -> 回收账户租金...")
                await asyncio.sleep(2) 
                if await trader.close_token_account(token_mint):
                    logger.info(f"  ♻️ 租金已回收 (+0.002 SOL)")
                
                sold_tokens.append(token_mint)
            else:
                logger.error(f"  ❌ 卖出失败，跳过")

            print("-" * 30)
            await asyncio.sleep(1)

    finally:
        # 4. 更新持仓文件
        if sold_tokens:
            for t in sold_tokens:
                if t in portfolio: del portfolio[t]
            
            with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f:
                json.dump(portfolio, f, indent=4)
        
        # --- 🔥 新增：计算并打印最终收益 ---
        try:
            end_bal_resp = await trader.rpc_client.get_balance(trader.payer.pubkey())
            end_balance = end_bal_resp.value / 10**9
            
            net_gained = end_balance - start_balance
            
            print("\n" + "="*50)
            logger.info(f"🏁 清仓结束！统计如下:")
            logger.info(f"💵 初始余额: {start_balance:.4f} SOL")
            logger.info(f"💰 当前余额: {end_balance:.4f} SOL")
            
            if net_gained >= 0:
                logger.info(f"📈 本次清仓回血: +{net_gained:.4f} SOL (含租金回收)")
            else:
                # 理论上不太可能，除非 Gas 费 > 卖出价值
                logger.info(f"📉 本次清仓变动: {net_gained:.4f} SOL")
            print("="*50 + "\n")
            
        except Exception:
            pass
        # -------------------------------

        await trader.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 用户强制停止")
