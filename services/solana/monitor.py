#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : monitor.py
@Description: 智能监控核心 (V5 Ultimate: WS强制保活 + HTTP轮询兜底 + 调试全开)
"""
import asyncio
import json
import traceback
import aiohttp
import websockets
from config.settings import WSS_ENDPOINT, TARGET_WALLET, HTTP_ENDPOINT, HELIUS_API_KEY
from utils.logger import logger

# 黑名单：忽略 SOL, USDC, USDT
IGNORE_MINTS = [
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
]


# --- 核心功能 1: 交易解析 ---

def parse_tx(tx_data):
    """ 解析交易数据，提取买卖信息 """
    if not tx_data: return None

    token_transfers = tx_data.get('tokenTransfers', [])
    native_transfers = tx_data.get('nativeTransfers', [])

    trade_info = {
        "action": "UNKNOWN",
        "token_address": None,
        "amount": 0,
        "sol_spent": 0.0
    }

    out_tokens = []
    in_tokens = []

    # 1. 分析 Token 流向
    for tx in token_transfers:
        mint = tx['mint']
        if mint in IGNORE_MINTS: continue

        if tx['fromUserAccount'] == TARGET_WALLET:
            out_tokens.append((mint, tx['tokenAmount']))
        elif tx['toUserAccount'] == TARGET_WALLET:
            in_tokens.append((mint, tx['tokenAmount']))

    # 2. 分析 SOL 变动 (计算成本)
    sol_change = 0
    for nt in native_transfers:
        if nt['fromUserAccount'] == TARGET_WALLET:
            sol_change -= nt['amount']
        elif nt['toUserAccount'] == TARGET_WALLET:
            sol_change += nt['amount']

    if sol_change < 0:
        trade_info['sol_spent'] = abs(sol_change) / 10 ** 9

    # 3. 判定买卖方向
    if in_tokens:
        trade_info['action'] = "BUY"
        trade_info['token_address'] = in_tokens[0][0]
        trade_info['amount'] = in_tokens[0][1]
    elif out_tokens:
        trade_info['action'] = "SELL"
        trade_info['token_address'] = out_tokens[0][0]
        trade_info['amount'] = out_tokens[0][1]

    return trade_info


# --- 核心功能 2: HTTP 数据拉取 (含重试与轮询) ---

async def fetch_transaction_details(session, signature):
    """
    [重试机制] 通过 HTTP 获取交易详情
    用于 WebSocket 推送后的详细数据补充
    """
    payload = {
        "transactions": [signature],
        "commitment": "confirmed",  # 详情查询用 confirmed 比较稳
        "encoding": "jsonParsed"
    }
    max_retries = 5  # 增加重试次数

    for i in range(max_retries):
        try:
            async with session.post(HTTP_ENDPOINT, json=payload, timeout=15) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and len(data) > 0:
                        return data[0]
                    else:
                        logger.debug(f"⚠️ [Attempt {i + 1}] Helius 尚未索引到 {signature[:8]}... 等待中")
                elif response.status == 429:
                    logger.warning(f"⚠️ [Attempt {i + 1}] API 限流 (429)，退避 2s...")
                    await asyncio.sleep(2)
                else:
                    logger.error(f"❌ [Attempt {i + 1}] API 请求失败: {response.status}")
        except Exception as e:
            logger.error(f"❌ [Attempt {i + 1}] 网络异常: {e}")

        # 指数退避：1s, 2s, 4s, 8s...
        await asyncio.sleep(1 * (2 ** i))

    logger.error(f"💀 最终放弃：交易 {signature} 详情抓取失败")
    return None


async def fetch_recent_transactions(session, limit=10):
    """
    [兜底机制] 主动轮询最近的 N 笔交易
    用于防止 WebSocket 断连导致的漏单
    """
    # 注意：这里需要直接拼接 URL，因为 HTTP_ENDPOINT 是 POST 用的
    url = f"https://api.helius.xyz/v0/addresses/{TARGET_WALLET}/transactions"
    params = {
        "api-key": HELIUS_API_KEY,
        "type": "SWAP",  # 只查 Swap，节省流量
        "limit": str(limit)
    }

    try:
        async with session.get(url, params=params, timeout=10) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logger.warning(f"⚠️ [轮询失败] HTTP {resp.status}")
                return []
    except Exception as e:
        logger.error(f"⚠️ [轮询异常] {e}")
        return []


# --- 核心功能 3: WebSocket 实时监控 ---

async def start_monitor(process_callback, pm):
    """
    启动WebSocket监控 (V5 Ultimate)
    集成：强制握手确认 + 超频心跳 + 失败过滤
    """
    async with aiohttp.ClientSession(trust_env=True) as session:
        while True:
            try:
                logger.info(f"🔗 [V5] 连接 WebSocket: Helius RPC (目标: {TARGET_WALLET[:6]})...")

                async with websockets.connect(
                        WSS_ENDPOINT,
                        ping_interval=15,  # 🔥 超高频心跳 (每15秒)，防止僵尸连接
                        ping_timeout=10,   # 10秒没回 pong 就视为断开
                        close_timeout=5,
                        max_size=None
                ) as ws:

                    # 1. 发送订阅请求
                    req_id = 42  # 固定的请求ID方便识别
                    subscribe_msg = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [TARGET_WALLET]},
                            {"commitment": "processed"}  # 🔥 关键：用 processed 抢速度！
                        ]
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info("📤 订阅请求已发送，等待握手确认...")

                    # 2. 强制等待确认 (Strict Check)
                    # 如果 10 秒内服务器没回 "订阅成功"，直接重连
                    is_subscribed = False
                    try:
                        while not is_subscribed:
                            # 设置 10 秒超时
                            response = await asyncio.wait_for(ws.recv(), timeout=10.0)
                            data = json.loads(response)

                            # 检查是不是订阅响应
                            if data.get("id") == req_id and "result" in data:
                                logger.info(f"✅ 订阅握手成功! Subscription ID: {data['result']}")
                                is_subscribed = True
                            elif "method" in data:
                                # 可能还没确认就推消息了（罕见），先忽略
                                pass
                            else:
                                logger.warning(f"❓ 收到未知响应: {data}")

                    except asyncio.TimeoutError:
                        logger.error("❌ 订阅握手超时 (10s)！服务器无响应，准备重连...")
                        raise Exception("Handshake Timeout")

                    logger.info("👀 全网监控已开启，等待大哥发车...")

                    # 3. 主循环 (数据接收)
                    while True:
                        try:
                            # 阻塞接收，不设应用层超时 (依赖底层的 ping_timeout 保活)
                            msg = await ws.recv()
                            data = json.loads(msg)

                            # 处理心跳/系统消息
                            if "method" not in data:
                                continue

                            # 处理交易通知
                            if data["method"] == "logsNotification":
                                res = data['params']['result']
                                signature = res['value']['signature']
                                err = res['value'].get('err')

                                # 🔥 过滤失败交易 (Helius 会推送执行失败的交易)
                                if err:
                                    logger.debug(f"🚫 忽略失败交易: {signature[:8]} (On-Chain Error)")
                                    continue

                                logger.info(f"⚡ [捕获] 链上动作: {signature} >>> 正在处理")

                                # 异步回调处理 (Process Task)
                                asyncio.create_task(process_callback(session, signature, pm))

                        except websockets.exceptions.ConnectionClosed as e:
                            logger.warning(f"🔌 连接断开 (Code: {e.code}, Reason: {e.reason})")
                            break  # 跳出内层循环，触发外层重连
                        except Exception as e:
                            logger.error(f"💥 消息循环异常: {e}")
                            # 不退出循环，尝试处理下一条消息

            except Exception as e:
                logger.error(f"❌ WebSocket 全局异常: {e}")
                logger.info("🔄 3秒后重连...")
                await asyncio.sleep(3)
