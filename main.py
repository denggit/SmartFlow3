#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : main.py
@Description: 智能跟单机器人 (支持 --proxy 参数)
"""
import asyncio
import argparse
import os

from config.settings import RPC_URL, COPY_AMOUNT_SOL, SLIPPAGE_BUY, MIN_LIQUIDITY_USD, MIN_FDV, MAX_FDV
from core.portfolio import PortfolioManager
from services.risk_control import check_token_liquidity, check_is_honeypot
from services.solana.monitor import start_monitor, parse_tx, fetch_transaction_details
from services.solana.trader import SolanaTrader
from utils.logger import logger


async def process_tx_task(session, signature, pm: PortfolioManager):
    tx_detail = await fetch_transaction_details(session, signature)
    trade = parse_tx(tx_detail)
    if not trade or not trade['token_address']: return

    token = trade['token_address']

    if trade['action'] == "BUY":
        # --- 1. 基础风控 ---
        is_safe, liq, fdv = await check_token_liquidity(session, token)
        
        # ✅ 修复 Bug：这里变量名必须用 token，不能用 mint
        is_honeypot = await check_is_honeypot(session, token) 

        if not is_safe:
            logger.warning(f"⚠️ 无法获取数据或流动性过低: {token}")
            return

        if not is_honeypot:
            logger.warning(f"🚫 拦截貔貅盘: {token}")
            return

        # --- 🔥 新增：买入次数限制 ---
        buy_times = pm.get_buy_counts(token)
        if buy_times >= 3:
            logger.warning(f"🛑 [风控] {token} 已买入 {buy_times} 次，停止加仓")
            return
        
        # --- 2. 通过检查，执行买入 ---
        logger.info(f"🔍 体检通过: 池子 ${liq:,.0f} | 市值 ${fdv:,.0f} | 第 {buy_times + 1} 次买入")
        
        amount_in = int(COPY_AMOUNT_SOL * 10 ** 9)
        success, est_out = await pm.trader.execute_swap(
            pm.trader.SOL_MINT, token, amount_in, SLIPPAGE_BUY
        )
        if success:
            pm.add_position(token, est_out, amount_in)

    elif trade['action'] == "SELL":
        await pm.execute_proportional_sell(token, trade['amount'])


async def main():
    # 1. 初始化服务
    trader = SolanaTrader(RPC_URL)

    # 2. 初始化核心逻辑
    pm = PortfolioManager(trader)

    logger.info("🤖 机器人全系统启动...")

    # 3. 运行所有任务
    await asyncio.gather(
        pm.monitor_1000x_profit(),
        pm.monitor_sync_positions(),
        pm.schedule_daily_report(),
        start_monitor(process_tx_task, pm)
    )


if __name__ == "__main__":
    # 🔥 新增：参数解析逻辑
    parser = argparse.ArgumentParser(description='Solana Copy Trading Bot')
    parser.add_argument('--proxy', action='store_true', help='开启本地 Clash 代理 (http://127.0.0.1:7890)')
    args = parser.parse_args()

    if args.proxy:
        # 如果带了 --proxy，强制设置环境变量
        proxy_url = "http://127.0.0.1:7890"
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        logger.info(f"🌍 本地模式: 已启用代理 {proxy_url}")
    else:
        # 如果没带，不设置任何代理，适合云端直连
        logger.info("☁️ 云端模式: 直连无代理")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 程序停止")
