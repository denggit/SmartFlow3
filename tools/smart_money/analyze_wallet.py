#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : analyze_wallet.py
@Description: 智能钱包画像识别 V5 (优化版)
              - 修复代币归因逻辑：按代币数量比例分配成本/收益
              - 改进错误处理和日志记录
              - 增强价格查询健壮性（重试机制）
              - 优化 SOL/WSOL 合并逻辑
              - 使用类封装，提升代码可维护性
@Author     : Auto-generated
@Date       : 2026-02-01
"""
import argparse
import asyncio
import logging
import os
import statistics
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import aiohttp

# 导入配置
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import HELIUS_API_KEY, JUPITER_API_KEY

# === ⚙️ 基础配置 ===
TARGET_TX_COUNT = 20000
DEXSCREENER_CHUNK_SIZE = 30
DEXSCREENER_TIMEOUT = 30  # 增加超时时间到 30 秒
DEXSCREENER_MAX_RETRIES = 2  # 减少重试次数，避免等待太久
JUPITER_QUOTE_TIMEOUT = 10  # Jupiter API 超时时间
JUPITER_MAX_RETRIES = 2
MIN_COST_THRESHOLD = 0.05  # 最小成本阈值，低于此值的代币不参与分析
WSOL_MINT = "So11111111111111111111111111111111111111112"  # WSOL 地址

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TransactionParser:
    """
    交易解析器：负责解析单笔交易中的 SOL 和代币变动
    
    职责：
    - 统计原生 SOL 变动
    - 统计 WSOL 变动
    - 统计其他代币变动
    - 合并 SOL/WSOL 避免重复计算
    """
    
    def __init__(self, target_wallet: str, wsol_mint: str = WSOL_MINT):
        """
        初始化交易解析器
        
        Args:
            target_wallet: 目标钱包地址
            wsol_mint: WSOL 代币地址
        """
        self.target_wallet = target_wallet
        self.wsol_mint = wsol_mint
    
    def parse_transaction(self, tx: dict) -> Tuple[float, Dict[str, float]]:
        """
        解析单笔交易，返回 SOL 净变动和代币变动
        
        Args:
            tx: 交易数据字典
            
        Returns:
            (sol_change, token_changes): SOL 净变动和代币变动字典
        """
        timestamp = tx.get('timestamp', 0)
        native_sol_change = 0.0
        wsol_change = 0.0
        token_changes = defaultdict(float)
        
        # 1. 统计原生 SOL 变动
        for nt in tx.get('nativeTransfers', []):
            if nt.get('fromUserAccount') == self.target_wallet:
                native_sol_change -= nt.get('amount', 0) / 1e9
            if nt.get('toUserAccount') == self.target_wallet:
                native_sol_change += nt.get('amount', 0) / 1e9
        
        # 2. 统计 WSOL 和其他代币变动
        for tt in tx.get('tokenTransfers', []):
            mint = tt.get('mint', '')
            amt = tt.get('tokenAmount', 0)
            
            if mint == self.wsol_mint:
                if tt.get('fromUserAccount') == self.target_wallet:
                    wsol_change -= amt
                if tt.get('toUserAccount') == self.target_wallet:
                    wsol_change += amt
            else:
                if tt.get('fromUserAccount') == self.target_wallet:
                    token_changes[mint] -= amt
                if tt.get('toUserAccount') == self.target_wallet:
                    token_changes[mint] += amt
        
        # 3. 合并 SOL/WSOL，避免重复计算
        sol_change = self._merge_sol_changes(native_sol_change, wsol_change)
        
        return sol_change, dict(token_changes), timestamp
    
    def _merge_sol_changes(self, native_sol: float, wsol: float) -> float:
        """
        合并原生 SOL 和 WSOL 变动，避免重复计算
        
        策略：
        - 如果同向变动（都是入或都是出），取绝对值较大的（可能是包装/解包操作）
        - 如果反向变动，直接相加（正常交易）
        - 如果只有一个有变动，直接返回该值
        
        Args:
            native_sol: 原生 SOL 变动
            wsol: WSOL 变动
            
        Returns:
            合并后的 SOL 净变动
        """
        # 如果其中一个为 0，直接返回另一个
        if abs(native_sol) < 1e-9:
            return wsol
        if abs(wsol) < 1e-9:
            return native_sol
        
        # 同向变动：可能是包装/解包操作，取绝对值较大的
        if native_sol * wsol > 0:
            return native_sol if abs(native_sol) > abs(wsol) else wsol
        
        # 反向变动：正常交易，直接相加
        return native_sol + wsol


class TokenAttributionCalculator:
    """
    代币归因计算器：负责将 SOL 成本/收益按比例分配给多个代币
    
    职责：
    - 按代币数量比例分配成本（买入时）
    - 按代币数量比例分配收益（卖出时）
    """
    
    @staticmethod
    def calculate_attribution(
        sol_change: float,
        token_changes: Dict[str, float]
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        """
        计算代币归因：按代币数量比例分配 SOL 成本/收益
        
        Args:
            sol_change: SOL 净变动（负数为支出，正数为收入）
            token_changes: 代币变动字典 {mint: amount}
            
        Returns:
            (buy_attributions, sell_attributions): 买入和卖出的 SOL 归因字典
        """
        buy_attributions = {}
        sell_attributions = {}
        
        if abs(sol_change) < 1e-9:
            return buy_attributions, sell_attributions
        
        # 分离买入和卖出
        buys = {mint: amt for mint, amt in token_changes.items() if amt > 0}
        sells = {mint: abs(amt) for mint, amt in token_changes.items() if amt < 0}
        
        if sol_change < 0:  # 支出 SOL -> 买入成本
            total_buy_tokens = sum(buys.values())
            if total_buy_tokens > 0:
                cost_per_token = abs(sol_change) / total_buy_tokens
                for mint, token_amount in buys.items():
                    buy_attributions[mint] = cost_per_token * token_amount
        
        elif sol_change > 0:  # 收入 SOL -> 卖出收益
            total_sell_tokens = sum(sells.values())
            if total_sell_tokens > 0:
                proceeds_per_token = sol_change / total_sell_tokens
                for mint, token_amount in sells.items():
                    sell_attributions[mint] = proceeds_per_token * token_amount
        
        return buy_attributions, sell_attributions


class PriceFetcher:
    """
    价格获取器：负责获取代币价格（直接获取 SOL 价格，无需 USD 转换）
    
    职责：
    - 使用 Jupiter API 直接获取代币对 SOL 的价格
    - 实现重试机制
    - 处理价格缺失情况
    """
    
    def __init__(self, session: aiohttp.ClientSession, jupiter_api_key: str = None):
        """
        初始化价格获取器
        
        Args:
            session: aiohttp 会话对象
            jupiter_api_key: Jupiter API 密钥（可选）
        """
        self.session = session
        self.jupiter_api_key = jupiter_api_key or JUPITER_API_KEY
        self._price_cache: Dict[str, float] = {}  # 缓存代币的 SOL 价格
    
    async def get_token_prices_in_sol(
        self,
        token_mints: List[str],
        max_retries: int = JUPITER_MAX_RETRIES
    ) -> Dict[str, float]:
        """
        批量获取代币对 SOL 的价格（直接获取，无需 USD 转换）
        
        Args:
            token_mints: 代币地址列表
            max_retries: 最大重试次数
            
        Returns:
            价格字典 {mint: price_sol}，表示 1 个代币 = 多少 SOL
        """
        if not token_mints:
            return {}
        
        prices = {}
        mints_list = list(set(token_mints))  # 去重
        
        # 使用 Jupiter API 并发获取价格
        tasks = [self._get_single_token_price_sol(mint, max_retries) for mint in mints_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for mint, result in zip(mints_list, results):
            if isinstance(result, Exception):
                logger.debug(f"获取 {mint[:8]}... 价格失败: {result}")
                continue
            if result is not None and result > 0:
                prices[mint] = result
                self._price_cache[mint] = result
        
        return prices
    
    async def _get_single_token_price_sol(
        self,
        token_mint: str,
        max_retries: int
    ) -> Optional[float]:
        """
        获取单个代币对 SOL 的价格
        
        Args:
            token_mint: 代币地址
            max_retries: 最大重试次数
            
        Returns:
            代币的 SOL 价格（1 个代币 = 多少 SOL），失败返回 None
        """
        # 检查缓存
        if token_mint in self._price_cache:
            return self._price_cache[token_mint]
        
        # 如果是 WSOL，直接返回 1
        if token_mint == WSOL_MINT:
            return 1.0
        
        # 使用 Jupiter API 询价：尝试不同的 decimals
        # 大多数代币使用 6 或 9 位小数，我们尝试几种常见值
        test_amounts = [
            int(1e6),   # 1 个代币（6 位小数）
            int(1e9),  # 1 个代币（9 位小数）
            int(1e8),  # 1 个代币（8 位小数）
        ]
        
        url = "https://api.jup.ag/swap/v1/quote"
        headers = {"Accept": "application/json"}
        if self.jupiter_api_key:
            headers["x-api-key"] = self.jupiter_api_key
        
        timeout = aiohttp.ClientTimeout(total=JUPITER_QUOTE_TIMEOUT)
        
        for quote_amount in test_amounts:
            params = {
                "inputMint": token_mint,
                "outputMint": WSOL_MINT,
                "amount": str(quote_amount),
                "slippageBps": "50",
                "onlyDirectRoutes": "false",
            }
            
            for attempt in range(max_retries):
                try:
                    async with self.session.get(url, params=params, headers=headers, timeout=timeout) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            out_amount = int(data.get('outAmount', 0))
                            if out_amount > 0:
                                # 计算价格：out_amount (lamports) / quote_amount (代币原始单位)
                                # out_amount 是 lamports，需要除以 1e9 得到 SOL
                                # quote_amount 是代币的原始单位，需要除以对应的 decimals 得到代币数量
                                decimals = 6 if quote_amount == int(1e6) else (9 if quote_amount == int(1e9) else 8)
                                price_sol = (out_amount / 1e9) / (quote_amount / (10 ** decimals))
                                # 如果价格合理（在 0.000001 到 1000 SOL 之间），返回
                                if 0.000001 <= price_sol <= 1000:
                                    return price_sol
                        elif resp.status == 429:
                            wait_time = (attempt + 1) * 2
                            logger.debug(f"Jupiter rate limited, waiting {wait_time}s")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            if attempt < max_retries - 1:
                                await asyncio.sleep(1)
                            break  # 尝试下一个 amount
                except asyncio.TimeoutError:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                    else:
                        break  # 尝试下一个 amount
                except Exception as e:
                    logger.debug(f"Jupiter API error for {token_mint[:8]}...: {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                    else:
                        break  # 尝试下一个 amount
        
        return None
    
    async def _fetch_chunk_prices(
        self,
        chunk: List[str],
        max_retries: int
    ) -> Dict[str, float]:
        """
        获取一批代币的价格（带重试）
        
        Args:
            chunk: 代币地址列表（最多 30 个）
            max_retries: 最大重试次数
            
        Returns:
            价格字典
        """
        url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
        
        # 使用更长的超时时间，但减少重试次数
        timeout = aiohttp.ClientTimeout(total=DEXSCREENER_TIMEOUT, connect=10)
        
        for attempt in range(max_retries):
            try:
                async with self.session.get(url, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pairs = data.get('pairs', [])
                        prices = {}
                        for p in pairs:
                            if p.get('chainId') == 'solana':
                                mint = p.get('baseToken', {}).get('address', '')
                                price = p.get('priceUsd', 0)
                                if mint and price:
                                    try:
                                        prices[mint] = float(price)
                                    except (ValueError, TypeError):
                                        continue
                        if prices:
                            logger.debug(f"成功获取 {len(prices)} 个代币价格")
                        return prices
                    elif resp.status == 429:
                        wait_time = (attempt + 1) * 3
                        logger.warning(f"Rate limited, waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logger.warning(f"API returned status {resp.status} for chunk")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2)
                        continue
            except asyncio.TimeoutError:
                logger.warning(f"Timeout fetching prices (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
            except aiohttp.ClientError as e:
                logger.warning(f"Network error fetching prices: {e} (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Unexpected error fetching prices: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
        
        logger.warning(f"无法获取价格数据，将使用已实现收益进行分析")
        return {}
    
    # 保留旧方法以保持向后兼容（但不再使用）
    async def get_current_prices(
        self,
        token_mints: List[str],
        max_retries: int = DEXSCREENER_MAX_RETRIES
    ) -> Dict[str, float]:
        """
        批量获取代币当前价格（USD）- 已废弃，请使用 get_token_prices_in_sol
        
        Args:
            token_mints: 代币地址列表
            max_retries: 最大重试次数
            
        Returns:
            价格字典 {mint: price_usd}
        """
        # 这个方法保留是为了向后兼容，但实际应该使用 get_token_prices_in_sol
        logger.warning("get_current_prices 已废弃，请使用 get_token_prices_in_sol")
        return await self.get_token_prices_in_sol(token_mints, max_retries)


class WalletAnalyzer:
    """
    钱包分析器：核心分析引擎
    
    职责：
    - 获取交易历史
    - 解析交易并计算代币项目收益
    - 生成分析报告
    """
    
    def __init__(self, helius_api_key: str = None):
        """
        初始化钱包分析器
        
        Args:
            helius_api_key: Helius API 密钥，如果为 None 则从配置读取
        """
        self.helius_api_key = helius_api_key or HELIUS_API_KEY
        if not self.helius_api_key:
            raise ValueError("HELIUS_API_KEY 未配置")
    
    async def fetch_history_pagination(
        self,
        session: aiohttp.ClientSession,
        address: str,
        max_count: int = 3000
    ) -> List[dict]:
        """
        分页获取钱包交易历史
        
        Args:
            session: aiohttp 会话对象
            address: 钱包地址
            max_count: 最大获取数量
            
        Returns:
            交易列表
        """
        all_txs = []
        last_signature = None
        retry_count = 0
        max_retries = 5
        
        while len(all_txs) < max_count:
            url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
            params = {
                "api-key": self.helius_api_key,
                "type": "SWAP",
                "limit": 100
            }
            if last_signature:
                params["before"] = last_signature
            
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        retry_count += 1
                        if retry_count > max_retries:
                            logger.warning(f"Rate limit exceeded, stopping at {len(all_txs)} transactions")
                            break
                        wait_time = retry_count * 2
                        logger.info(f"Rate limited, waiting {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue
                    
                    if resp.status != 200:
                        logger.warning(f"API returned status {resp.status}, stopping")
                        break
                    
                    data = await resp.json()
                    if not data:
                        break
                    
                    all_txs.extend(data)
                    if len(data) < 100:
                        break
                    
                    last_signature = data[-1].get('signature')
                    retry_count = 0  # 重置重试计数
                    await asyncio.sleep(0.1)
                    
            except aiohttp.ClientError as e:
                logger.error(f"Network error fetching transactions: {e}")
                break
            except Exception as e:
                logger.error(f"Unexpected error fetching transactions: {e}")
                break
        
        return all_txs[:max_count]
    
    async def parse_token_projects(
        self,
        session: aiohttp.ClientSession,
        transactions: List[dict],
        target_wallet: str
    ) -> List[dict]:
        """
        解析交易并计算每个代币项目的收益
        
        Args:
            session: aiohttp 会话对象
            transactions: 交易列表
            target_wallet: 目标钱包地址
            
        Returns:
            代币项目分析结果列表
        """
        # 初始化组件
        parser = TransactionParser(target_wallet)
        attribution_calc = TokenAttributionCalculator()
        price_fetcher = PriceFetcher(session)
        
        # 项目数据：{mint: {buy_sol, sell_sol, buy_tokens, sell_tokens, first_time, last_time}}
        projects = defaultdict(lambda: {
            "buy_sol": 0.0,
            "sell_sol": 0.0,
            "buy_tokens": 0.0,
            "sell_tokens": 0.0,
            "first_time": 0,
            "last_time": 0
        })
        
        # 按时间倒序处理交易（从最早到最新）
        for tx in reversed(transactions):
            try:
                # 解析交易
                sol_change, token_changes, timestamp = parser.parse_transaction(tx)
                
                # 计算归因
                buy_attributions, sell_attributions = attribution_calc.calculate_attribution(
                    sol_change, token_changes
                )
                
                # 更新项目数据
                for mint, delta in token_changes.items():
                    # 更新代币数量
                    if delta > 0:
                        projects[mint]["buy_tokens"] += delta
                    else:
                        projects[mint]["sell_tokens"] += abs(delta)
                    
                    # 更新 SOL 成本/收益
                    if mint in buy_attributions:
                        projects[mint]["buy_sol"] += buy_attributions[mint]
                    if mint in sell_attributions:
                        projects[mint]["sell_sol"] += sell_attributions[mint]
                    
                    # 更新时间戳
                    if projects[mint]["first_time"] == 0 and timestamp > 0:
                        projects[mint]["first_time"] = timestamp
                    if timestamp > 0:
                        projects[mint]["last_time"] = timestamp
                
                # 处理无 SOL 交易的跨代币兑换
                if abs(sol_change) < 1e-9 and token_changes:
                    # 跨代币兑换：只记录代币数量，不记录 SOL
                    for mint, delta in token_changes.items():
                        if delta > 0:
                            projects[mint]["buy_tokens"] += delta
                        else:
                            projects[mint]["sell_tokens"] += abs(delta)
                            
            except Exception as e:
                logger.warning(f"Error parsing transaction: {e}")
                continue
        
        # 获取当前价格并计算最终收益（直接获取 SOL 价格，无需 USD 转换）
        active_mints = [
            m for m, v in projects.items()
            if (v["buy_tokens"] - v["sell_tokens"]) > 0 and v["buy_sol"] >= MIN_COST_THRESHOLD
        ]
        
        logger.info(f"正在获取 {len(active_mints)} 个代币的 SOL 价格...")
        prices_sol = await price_fetcher.get_token_prices_in_sol(active_mints)
        
        # 统计价格获取情况
        prices_found = len(prices_sol)
        if prices_found < len(active_mints):
            missing_count = len(active_mints) - prices_found
            logger.warning(f"价格查询完成: 成功 {prices_found}/{len(active_mints)}，缺失 {missing_count} 个代币价格")
        
        # 生成最终结果
        final_results = []
        for mint, data in projects.items():
            if data["buy_sol"] < MIN_COST_THRESHOLD:
                continue
            
            remaining_tokens = max(0, data["buy_tokens"] - data["sell_tokens"])
            price_sol = prices_sol.get(mint, 0)
            
            # 如果价格缺失，只计算已实现收益
            if price_sol == 0 and remaining_tokens > 0:
                logger.debug(f"代币 {mint[:8]}... 价格缺失，仅计算已实现收益")
                unrealized_sol = 0  # 价格未知时，未实现收益为 0
            else:
                unrealized_sol = remaining_tokens * price_sol
            
            total_value_sol = data["sell_sol"] + unrealized_sol
            net_profit = total_value_sol - data["buy_sol"]
            roi = (total_value_sol / data["buy_sol"] - 1) if data["buy_sol"] > 0 else 0
            exit_pct = data["sell_tokens"] / data["buy_tokens"] if data["buy_tokens"] > 0 else 0
            
            hold_time_minutes = 0
            if data["last_time"] > 0 and data["first_time"] > 0:
                hold_time_minutes = (data["last_time"] - data["first_time"]) / 60
            
            final_results.append({
                "token": mint,
                "cost": data["buy_sol"],
                "profit": net_profit,
                "roi": roi,
                "is_win": net_profit > 0,
                "hold_time": hold_time_minutes,
                "exit_status": f"{exit_pct:.0%}",
                "has_price": price_sol > 0  # 标记是否有价格数据
            })
        
        return final_results


def get_detailed_scores(results: List[dict]) -> Tuple[int, str, str, Dict[str, int]]:
    """
    计算钱包详细评分和雷达图数据
    
    Args:
        results: 代币项目分析结果列表
        
    Returns:
        (final_score, tier, description, radar): 
        - final_score: 综合评分
        - tier: 评级 (S/A/B/F)
        - description: 状态描述
        - radar: 雷达图数据字典
    """
    if not results:
        return 0, "F", "无数据", {}
    
    count = len(results)
    wins = [r for r in results if r.get('is_win', False)]
    win_rate = len(wins) / count if count > 0 else 0
    
    total_profit = sum(r.get('profit', 0) for r in results)
    hold_times = [r.get('hold_time', 0) for r in results if r.get('hold_time', 0) > 0]
    median_hold = statistics.median(hold_times) if hold_times else 0
    
    avg_win = sum(r.get('profit', 0) for r in wins) / len(wins) if wins else 0
    losses = [r for r in results if not r.get('is_win', False)]
    avg_loss = abs(sum(r.get('profit', 0) for r in losses) / len(losses)) if losses else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else (avg_win if avg_win > 0 else 0)
    
    # 基础评分
    base_score = 100
    if win_rate < 0.4:
        base_score -= 30
    elif win_rate > 0.6:
        base_score += 10
    
    # 置信度乘数
    conf_multiplier = 0.3 if count < 5 else (0.7 if count < 10 else 1.0)
    
    # 雷达图评分
    radar = {
        "🛡️ 稳健中军": int(max(0, base_score - (30 if median_hold < 10 else 0)) * conf_multiplier),
        "⚔️ 土狗猎手": int(max(0, base_score + (20 if profit_factor > 3 else 0)) * conf_multiplier),
        "💎 钻石之手": int(max(0, base_score - (40 if median_hold < 60 else 0)) * conf_multiplier)
    }
    
    final_score = max(radar.values()) if radar else 0
    tier = "S" if final_score >= 100 else ("A" if final_score >= 85 else ("B" if final_score >= 70 else "F"))
    description = f"盈亏比: {profit_factor:.2f} | 代币数: {count}"
    
    return final_score, tier, description, radar


# 导出函数（保持向后兼容）
async def fetch_history_pagination(session, address, max_count=3000):
    """向后兼容函数"""
    analyzer = WalletAnalyzer()
    return await analyzer.fetch_history_pagination(session, address, max_count)


async def parse_token_projects(session, transactions, target_wallet):
    """向后兼容函数"""
    analyzer = WalletAnalyzer()
    return await analyzer.parse_token_projects(session, transactions, target_wallet)


async def main():
    """主函数：命令行入口"""
    parser = argparse.ArgumentParser(description="智能钱包画像识别工具")
    parser.add_argument("wallet", help="钱包地址")
    parser.add_argument("--max-txs", type=int, default=TARGET_TX_COUNT, help="最大交易数量")
    args = parser.parse_args()
    
    analyzer = WalletAnalyzer()
    
    async with aiohttp.ClientSession() as session:
        print(f"🔍 正在深度审计 V5: {args.wallet[:6]}...")
        txs = await analyzer.fetch_history_pagination(session, args.wallet, args.max_txs)
        
        if not txs:
            print("❌ 未获取到交易数据")
            return
        
        print(f"📊 获取到 {len(txs)} 笔交易，开始分析...")
        results = await analyzer.parse_token_projects(session, txs, args.wallet)
        
        if not results:
            print("❌ 未找到有效的代币项目")
            return
        
        score, tier, desc, radar = get_detailed_scores(results)
        
        print("\n" + "═" * 60)
        print(f"🧬 战力报告 (V5): {args.wallet[:6]}...")
        print("═" * 60)
        
        wins = [r for r in results if r['is_win']]
        win_rate = len(wins) / len(results) if results else 0
        total_profit = sum(r['profit'] for r in results)
        hold_times = [r['hold_time'] for r in results if r['hold_time'] > 0]
        median_hold = statistics.median(hold_times) if hold_times else 0
        
        print(f"📊 核心汇总:")
        print(f"   • 项目胜率: {win_rate:.1%} (基于 {len(results)} 个代币)")
        print(f"   • 累计利润: {total_profit:+,.2f} SOL")
        print(f"   • 持仓中位: {median_hold:.1f} 分钟")
        
        confidence = "高" if len(results) > 10 else "低"
        print("-" * 30)
        print(f"🎯 战力雷达 (置信度: {confidence}):")
        for role, sc in radar.items():
            bar_length = sc // 10
            bar = '█' * bar_length + '░' * (10 - bar_length)
            print(f"   {role}: {bar} {sc}分")
        
        print("-" * 30)
        print(f"🏆 综合评级: [{tier}级] {score} 分")
        print(f"📝 状态评价: {desc}")
        print("-" * 30)
        
        print("\n📝 重点项目明细 (按利润排序):")
        results_sorted = sorted(results, key=lambda x: x['profit'], reverse=True)
        for r in results_sorted[:8]:
            status_icon = '🟢' if r['is_win'] else '🔴'
            token_short = r['token'][:6] + '..'
            profit = r['profit']
            roi_pct = r['roi'] * 100
            exit_status = r['exit_status']
            print(f" {status_icon} {token_short} | 利润 {profit:>+7.2f} | ROI {roi_pct:>+7.1f}% | 退出度 {exit_status}")


if __name__ == "__main__":
    asyncio.run(main())
