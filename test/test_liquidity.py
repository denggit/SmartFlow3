#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import asyncio
import aiohttp
import logging

# 配置简单的日志输出
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("Test")

# --- 1. 设置环境变量 (必须在创建 session 之前) ---
os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"

# --- 待测试的代币列表 ---
TEST_TOKENS = {
    "JUP (正常代币)": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "USDC (稳定币)": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "日志报错币1": "Gais2Ur4eywvEc3ZqnGxDs41UorzuAd8LGpZSHqbbonk",
    "日志报错币2": "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",
    "日志报错币3": "FH4ibsbhhHyb8NcR5gVw2xVvYMwHFuUcvAx6En9YRWHi"
}

async def check_token_liquidity(session, token_mint):
    # 忽略 SOL
    if token_mint == "So11111111111111111111111111111111111111112":
        return True, 999999999, 999999999

    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://dexscreener.com/"
    }

    try:
        print(f"正在请求: {url} ...")
        # 注意：这里不需要再手动传 proxy=... 参数了，session 会自动处理
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                pairs = data.get('pairs', [])

                if not pairs:
                    print(f"❌ 结果: DexScreener 未收录 (pairs为空)")
                    return False, 0, 0

                solana_pairs = [p for p in pairs if p.get('chainId') == 'solana']
                if not solana_pairs:
                    print(f"❌ 结果: 未找到 Solana 链上的池子")
                    return False, 0, 0

                best_pair = max(solana_pairs, key=lambda x: x.get('liquidity', {}).get('usd', 0))
                liq = best_pair.get('liquidity', {}).get('usd', 0)
                fdv = best_pair.get('fdv', 0)

                print(f"✅ 结果: 获取成功 | 流动性: ${liq:,.0f} | 市值: ${fdv:,.0f}")
                return True, liq, fdv
            else:
                print(f"❌ HTTP 错误: {response.status}")
    except Exception as e:
        print(f"❌ 代码报错: {e}")

    return False, 0, 0

async def main():
    # 🔥🔥🔥 关键修改：添加 trust_env=True
    async with aiohttp.ClientSession(trust_env=True) as session:
        print(f"=== 开始单元测试 (使用环境变量代理: {os.environ.get('HTTP_PROXY')}) ===\n")
        for name, mint in TEST_TOKENS.items():
            print(f"Testing [{name}]: {mint}")
            await check_token_liquidity(session, mint)
            print("-" * 30)

if __name__ == "__main__":
    asyncio.run(main())