#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : monitor.py
@Description: 
"""
import asyncio
# services/monitor.py
import json

import aiohttp
import websockets

from config.settings import WSS_ENDPOINT, TARGET_WALLET, HTTP_ENDPOINT
from utils.logger import logger

# 黑名单：忽略 SOL, USDC, USDT
IGNORE_MINTS = [
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
]


# ================= 辅助模块：交易解析 =================
async def fetch_transaction_details(session, signature):
    payload = {"transactions": [signature]}
    try:
        async with session.post(HTTP_ENDPOINT, json=payload) as response:
            if response.status == 200:
                data = await response.json()
                if len(data) > 0: return data[0]
    except Exception:
        pass
    return None


def parse_tx(tx_data):
    if not tx_data: return None
    
    # 1. 获取转账数据
    token_transfers = tx_data.get('tokenTransfers', [])
    native_transfers = tx_data.get('nativeTransfers', []) # 🔥 新增：获取 SOL 转账记录
    
    # 初始化返回结构，新增 sol_spent 字段
    trade_info = {
        "action": "UNKNOWN", 
        "token_address": None, 
        "amount": 0,
        "sol_spent": 0.0  # 🔥 新增：记录这笔交易花了多少 SOL
    }

    out_tokens = []
    in_tokens = []

    # 🚫 黑名单：忽略 SOL, USDC, USDT
    IGNORE_MINTS = [
        "So11111111111111111111111111111111111111112",  # WSOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    ]

    for tx in token_transfers:
        mint = tx['mint']
        if mint in IGNORE_MINTS: continue

        if tx['fromUserAccount'] == TARGET_WALLET:
            out_tokens.append((mint, tx['tokenAmount']))
        elif tx['toUserAccount'] == TARGET_WALLET:
            in_tokens.append((mint, tx['tokenAmount']))

    # 🔥🔥🔥 新增：计算 SOL 变动 (判断大哥花了多少钱) 🔥🔥🔥
    sol_change = 0
    for nt in native_transfers:
        if nt['fromUserAccount'] == TARGET_WALLET:
            sol_change -= nt['amount'] # 流出 (花钱)
        elif nt['toUserAccount'] == TARGET_WALLET:
            sol_change += nt['amount'] # 流入 (收钱)
            
    # 如果 sol_change 是负数，说明是净流出（买入成本）
    if sol_change < 0:
        trade_info['sol_spent'] = abs(sol_change) / 10**9
    # -----------------------------------------------------

    if in_tokens:
        trade_info['action'] = "BUY"
        trade_info['token_address'] = in_tokens[0][0]
        trade_info['amount'] = in_tokens[0][1]
    elif out_tokens:
        trade_info['action'] = "SELL"
        trade_info['token_address'] = out_tokens[0][0]
        trade_info['amount'] = out_tokens[0][1]

    return trade_info


async def start_monitor(process_callback, pm):
    """
    process_callback: 这是一个回调函数，当解析出交易时调用它
    """
    async with aiohttp.ClientSession(trust_env=True) as session:
        while True:
            try:
                logger.info(f"🔗 连接 WebSocket: {TARGET_WALLET}...")
                async with websockets.connect(WSS_ENDPOINT, ping_interval=30, ping_timeout=60) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                        "params": [{"mentions": [TARGET_WALLET]}, {"commitment": "processed"}]
                    }))
                    logger.info("👀 监控已就绪...")
                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        if "method" in data and data["method"] == "logsNotification":
                            res = data['params']['result']
                            if any("Swap" in log for log in res['value']['logs']):
                                signature = res['value']['signature']
                                # 这里调用传入的回调函数，解耦逻辑
                                asyncio.create_task(process_callback(session, signature, pm))
            except Exception as e:
                logger.error(f"❌ 连接断开: {e}, 3秒后重连...")
                await asyncio.sleep(3)
