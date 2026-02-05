#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : services/solana/trader.py
@Description: SOL 交易执行模块 (集成 Jito MEV 防夹 + SSL 修复版)
"""
import base64
import os
import random
import asyncio
import traceback
import base58  # 🔥 需要 pip install base58
import aiohttp
import httpx
from dotenv import load_dotenv

from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts, TokenAccountOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.system_program import transfer, TransferParams
from spl.token.instructions import close_account, CloseAccountParams
from spl.token.constants import TOKEN_PROGRAM_ID

# 引入新配置
from config.settings import (
    PRIVATE_KEY,
    USE_JITO,
    JITO_TIP_AMOUNT,
    JITO_BLOCK_ENGINE_URL,
    JITO_TIP_ACCOUNTS
)
from utils.logger import logger

load_dotenv()


class SolanaTrader:
    def __init__(self, rpc_endpoint):
        # 保持原有的 RPC 初始化逻辑 (配合下方的 SSL Patch)
        self.rpc_client = AsyncClient(rpc_endpoint, timeout=30)

        if not PRIVATE_KEY:
            raise ValueError("❌ 未找到私钥，请在 .env 或 config/settings.py 中配置 PRIVATE_KEY")

        try:
            if isinstance(PRIVATE_KEY, str):
                self.payer = Keypair.from_base58_string(PRIVATE_KEY)
            else:
                self.payer = Keypair.from_bytes(PRIVATE_KEY)
        except Exception as e:
            logger.error(f"私钥加载失败: {e}")
            raise e

        self.SOL_MINT = "So11111111111111111111111111111111111111112"

    async def get_token_balance(self, wallet_address: str, token_mint: str) -> float:
        """获取指定代币余额 (保留原逻辑)"""
        try:
            if token_mint == self.SOL_MINT:
                resp = await self.rpc_client.get_balance(Pubkey.from_string(wallet_address))
                return resp.value / 10 ** 9

            resp = await self.rpc_client.get_token_accounts_by_owner(
                Pubkey.from_string(wallet_address),
                {"mint": Pubkey.from_string(token_mint)}
            )
            if not resp.value:
                return 0.0

            account_data = resp.value[0].pubkey
            balance_resp = await self.rpc_client.get_token_account_balance(account_data)
            return float(balance_resp.value.ui_amount)
        except Exception as e:
            logger.error(f"获取余额失败: {e}")
            return 0.0

    async def get_token_balance_raw(self, wallet_address: str, token_mint: str) -> int:
        """获取代币原始余额 (保留净值法修复逻辑)"""
        try:
            if token_mint == self.SOL_MINT:
                return None

            resp = await self.rpc_client.get_token_accounts_by_owner(
                Pubkey.from_string(wallet_address),
                {"mint": Pubkey.from_string(token_mint)}
            )
            if not resp.value:
                return 0

            account_data = resp.value[0].pubkey
            balance_resp = await self.rpc_client.get_token_account_balance(account_data)
            return int(balance_resp.value.amount)
        except Exception as e:
            logger.warning(f"获取原始余额失败: {e}")
            return None

    async def get_quote(self, session, input_mint, output_mint, amount_lamports, slippage_bps=50):
        """从 Jupiter 获取报价 (保留原逻辑)"""
        url = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount_lamports,
            "slippageBps": slippage_bps
        }
        try:
            async with session.get(url, params=params) as response:
                return await response.json()
        except Exception as e:
            logger.error(f"询价失败: {e}")
            return None

    async def send_jito_bundle(self, jupiter_tx_bytes):
        """
        🚀 [新增] 发送 Jito Bundle (Jupiter Swap + 小费)
        """
        try:
            # 1. 解析 Jupiter 返回的交易
            swap_tx = VersionedTransaction.from_bytes(jupiter_tx_bytes)

            # 2. 构建小费交易 (Tip Transaction)
            tip_account = random.choice(JITO_TIP_ACCOUNTS)
            tip_lamports = int(JITO_TIP_AMOUNT * 10 ** 9)

            latest_blockhash = await self.rpc_client.get_latest_blockhash()
            blockhash = latest_blockhash.value.blockhash

            tip_ix = transfer(
                TransferParams(
                    from_pubkey=self.payer.pubkey(),
                    to_pubkey=Pubkey.from_string(tip_account),
                    lamports=tip_lamports
                )
            )

            tip_msg = MessageV0.try_compile(
                self.payer.pubkey(),
                [tip_ix],
                [],
                blockhash
            )
            tip_tx = VersionedTransaction(tip_msg, [self.payer])

            # 3. 重新签署两笔交易
            signed_swap_tx = VersionedTransaction(swap_tx.message, [self.payer])

            # 4. 编码为 Base58 (Jito API 要求)
            b58_swap = base58.b58encode(bytes(signed_swap_tx)).decode('utf-8')
            b58_tip = base58.b58encode(bytes(tip_tx)).decode('utf-8')

            # 5. 发送 Bundle
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendBundle",
                "params": [[b58_swap, b58_tip]]
            }

            logger.info(f"🚀 发送 Jito Bundle... (节点: {JITO_BLOCK_ENGINE_URL}, 小费: {JITO_TIP_AMOUNT} SOL)")

            async with aiohttp.ClientSession() as session:
                async with session.post(JITO_BLOCK_ENGINE_URL, json=payload) as resp:
                    data = await resp.json()
                    if "result" in data:
                        bundle_id = data["result"]
                        logger.info(f"✅ Jito Bundle 已提交! ID: {bundle_id}")
                        return True
                    else:
                        logger.error(f"❌ Jito 发送失败: {data}")
                        return False

        except Exception as e:
            logger.error(f"💥 Jito Bundle 构建异常: {e}")
            logger.error(traceback.format_exc())
            return False

    async def execute_swap(self, input_mint, output_mint, amount_lamports, slippage_bps=50):
        """
        执行 Swap 交易 (修改版：支持 Jito / 普通 RPC 切换)
        """
        async with aiohttp.ClientSession() as session:
            # 1. 询价
            quote = await self.get_quote(session, input_mint, output_mint, amount_lamports, slippage_bps)
            if not quote:
                return False, 0

            est_out = int(quote.get("outAmount", 0))

            # 2. 获取交易数据
            # 如果开启 Jito，不需要 Jupiter 加优先费(auto)，因为我们会自己付小费
            # 如果关闭 Jito，还是加上 auto 比较稳
            priority_fee = "auto" if not USE_JITO else 0

            swap_payload = {
                "quoteResponse": quote,
                "userPublicKey": str(self.payer.pubkey()),
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": priority_fee
            }

            try:
                async with session.post("https://quote-api.jup.ag/v6/swap", json=swap_payload) as response:
                    swap_resp = await response.json()
            except Exception as e:
                logger.error(f"Jupiter API 请求失败: {e}")
                return False, 0

            if "swapTransaction" not in swap_resp:
                logger.error(f"获取 Swap 交易失败: {swap_resp}")
                return False, 0

            swap_transaction_buf = base64.b64decode(swap_resp["swapTransaction"])

            # --- 分支逻辑：Jito vs 普通 RPC ---
            if USE_JITO:
                # 🅰️ Jito 模式
                success = await self.send_jito_bundle(swap_transaction_buf)
                if success:
                    # Jito 不返回即时结果，简单等待几秒认为上链
                    # 真实结果会由 Portfolio 的 sync_real_balance 最终确认
                    await asyncio.sleep(2)
                    return True, est_out
                else:
                    return False, 0
            else:
                # 🅱️ 普通 RPC 模式 (保留原文件逻辑)
                try:
                    tx = VersionedTransaction.from_bytes(swap_transaction_buf)
                    signed_tx = VersionedTransaction(tx.message, [self.payer])

                    opts = TxOpts(skip_preflight=True, max_retries=3)
                    signature = await self.rpc_client.send_transaction(signed_tx, opts=opts)
                    logger.info(f"📡 普通交易发送成功: {signature.value}")

                    await asyncio.sleep(2)
                    return True, est_out
                except Exception as e:
                    logger.error(f"普通交易执行异常: {e}")
                    return False, 0

    async def close_token_account(self, token_mint_str):
        """ 🔥 回收租金：关闭空的代币账户，拿回 0.002 SOL """
        try:
            # 1. 查找该代币的 ATA (关联账户)
            opts = TokenAccountOpts(mint=Pubkey.from_string(token_mint_str))
            resp = await self.rpc_client.get_token_accounts_by_owner(self.payer.pubkey(), opts)

            if not resp.value:
                logger.info(f"⚠️ 账户不存在，无需关闭: {token_mint_str}")
                return False

            token_account_pubkey = resp.value[0].pubkey

            # 2. 构建关闭指令 (CloseAccount)
            close_ix = close_account(
                CloseAccountParams(
                    account=token_account_pubkey,
                    dest=self.payer.pubkey(),
                    owner=self.payer.pubkey(),
                    program_id=TOKEN_PROGRAM_ID
                )
            )

            # 3. 构建并发送交易 (Versioned Transaction)
            # 获取最新的 blockhash
            latest_blockhash = await self.rpc_client.get_latest_blockhash()
            msg = MessageV0.try_compile(
                self.payer.pubkey(),
                [close_ix],
                [],
                latest_blockhash.value.blockhash,
            )
            vtx = VersionedTransaction(msg, [self.payer])

            opts = TxOpts(skip_preflight=True)
            await self.rpc_client.send_transaction(vtx, opts=opts)

            logger.info(f"♻️ [房租回收] 成功关闭账户，回血 +0.002 SOL")
            return True

        except Exception as e:
            logger.warning(f"⚠️ 关闭账户失败 (可能由粉尘残留导致): {e}")
            return False


# 🔥 Monkey Patch: 强制修改 httpx 的默认行为，使其不验证 SSL
# 这一步是为了解决 Solana RPC (httpx) 在代理下的报错问题
def patch_httpx_verify():
    original_init = httpx.AsyncClient.__init__

    def new_init(self, *args, **kwargs):
        kwargs['verify'] = False  # 强制关闭验证
        original_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = new_init


patch_httpx_verify()
