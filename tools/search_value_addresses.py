#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/4/26 8:28 PM
@File       : search_value_addresses.py
@Description: 从 DexScreener 拉取过去7天新上线的 Solana 代币，
              找出涨到10倍以上的代币，并获取在这些代币上赚取10倍以上的 top traders 钱包地址
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests

# 导入配置
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config.settings import HELIUS_API_KEY, RPC_URL

# --- 配置区 ---
BIRDEYE_API_KEY = "596803375980444db9ab5982db90763f"
N_DAYS = 7  # 检查过去 n 天
MULTIPLIER = 10  # 涨幅倍数
MIN_LIQUIDITY = 50000  # 流动性过滤（美元），太低的通常是极其不稳定的土狗
MIN_PROFIT_MULTIPLIER = 10  # 交易者盈利倍数阈值
TOP_TRADERS_LIMIT = 20  # 每个代币获取的 top traders 数量


class TokenAnalyzer:
    """
    代币分析器：负责分析代币价格涨幅和交易者盈利情况
    
    职责：
    - 从 DexScreener 获取新上线代币
    - 分析代币价格涨幅
    - 获取交易记录并分析交易者盈利
    """
    
    def __init__(self):
        """
        初始化代币分析器
        """
        self.birdeye_api_key = BIRDEYE_API_KEY
        self.helius_api_key = HELIUS_API_KEY
        self.rpc_url = RPC_URL
    
    def get_newly_listed_tokens(self) -> List[Dict]:
        """
        从 DexScreener 获取过去7天新上线的 Solana 代币
        
        Returns:
            新上线代币列表，每个代币包含 mint 地址、创建时间等信息
        """
        print("正在从 DexScreener 抓取过去7天新上线的 Solana 代币...")
        
        # 计算7天前的时间戳（毫秒）
        seven_days_ago = int((datetime.now() - timedelta(days=N_DAYS)).timestamp() * 1000)
        now_timestamp = int(datetime.now().timestamp() * 1000)
        
        newly_listed_tokens = []
        seen_mints = set()  # 用于去重
        all_pairs = []  # 用于调试
        
        try:
            # 方法1: 使用 DexScreener 的搜索接口获取 Solana 代币
            print("方法1: 使用搜索接口...")
            search_url = "https://api.dexscreener.com/latest/dex/search?q=solana"
            
            response = requests.get(search_url, timeout=30)
            if response.status_code == 200:
                data = response.json()
                pairs = data.get("pairs", [])
                all_pairs.extend(pairs)
                print(f"  从搜索接口获取到 {len(pairs)} 个交易对")
            
            # 方法2: 使用 token-boosts 接口获取热门代币
            print("方法2: 使用 token-boosts 接口...")
            try:
                boosts_url = "https://api.dexscreener.com/token-boosts/top/v1"
                response = requests.get(boosts_url, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    boosts_pairs = data.get("pairs", [])
                    all_pairs.extend(boosts_pairs)
                    print(f"  从 token-boosts 接口获取到 {len(boosts_pairs)} 个交易对")
            except Exception as e:
                print(f"  token-boosts 接口请求失败: {e}")
            
            # 方法3: 尝试获取 Solana 链上的最新交易对（如果有相关接口）
            # 注意：DexScreener 可能没有直接的新币列表接口，这里尝试其他方法
            
            if not all_pairs:
                print("未获取到任何交易对数据")
                return []

            # 去重（基于 pairAddress）
            unique_pairs = {}
            for pair in all_pairs:
                pair_addr = pair.get('pairAddress')
                if pair_addr and pair_addr not in unique_pairs:
                    unique_pairs[pair_addr] = pair
            
            all_pairs = list(unique_pairs.values())
            print(f"\n去重后共 {len(all_pairs)} 个唯一交易对，开始筛选...")
            
            # 统计信息
            stats = {
                'total': len(all_pairs),
                'solana_chain': 0,
                'has_created_at': 0,
                'within_7_days': 0,
                'has_mint': 0,
                'meets_liquidity': 0,
            }
            
            # 筛选 Solana 链上过去7天新上线的代币
            for pair in all_pairs:
                # 统计：Solana 链
                if pair.get('chainId') == 'solana':
                    stats['solana_chain'] += 1
                else:
                    continue
                
                # 获取创建时间
                pair_created_at = pair.get('pairCreatedAt')
                if not pair_created_at:
                    # 如果没有创建时间，跳过（无法判断是否新上线）
                    continue
                
                stats['has_created_at'] += 1
                
                # 转换时间戳（可能是毫秒或秒）
                if pair_created_at > 1e10:
                    created_timestamp = pair_created_at
                else:
                    created_timestamp = pair_created_at * 1000
                
                # 检查是否在过去7天内
                if not (seven_days_ago <= created_timestamp <= now_timestamp):
                    continue
                
                stats['within_7_days'] += 1
                
                # 获取代币信息
                base_token = pair.get('baseToken', {})
                mint_address = base_token.get('address')
                
                if not mint_address or mint_address in seen_mints:
                    continue
                
                stats['has_mint'] += 1
                seen_mints.add(mint_address)
                
                # 获取流动性（可能是数字或字典）
                liquidity_data = pair.get('liquidity', {})
                if isinstance(liquidity_data, dict):
                    liquidity_usd = liquidity_data.get('usd', 0)
                else:
                    liquidity_usd = liquidity_data if isinstance(liquidity_data, (int, float)) else 0
                
                token_info = {
                    'mint': mint_address,
                    'pair_address': pair.get('pairAddress'),
                    'symbol': base_token.get('symbol', 'Unknown'),
                    'name': base_token.get('name', 'Unknown'),
                    'created_at': created_timestamp,
                    'liquidity_usd': liquidity_usd,
                    'price_usd': pair.get('priceUsd', '0'),
                    'volume_24h': pair.get('volume', {}).get('h24', 0) if isinstance(pair.get('volume'), dict) else 0,
                }
                
                # 过滤流动性太低的代币
                if token_info['liquidity_usd'] and token_info['liquidity_usd'] >= MIN_LIQUIDITY:
                    stats['meets_liquidity'] += 1
                    newly_listed_tokens.append(token_info)
            
            # 输出统计信息
            print(f"\n筛选统计:")
            print(f"  总交易对数: {stats['total']}")
            print(f"  Solana 链: {stats['solana_chain']}")
            print(f"  有创建时间: {stats['has_created_at']}")
            print(f"  过去7天内: {stats['within_7_days']}")
            print(f"  有 mint 地址: {stats['has_mint']}")
            print(f"  流动性 >= {MIN_LIQUIDITY} USD: {stats['meets_liquidity']}")
            print(f"\n共筛选出 {len(newly_listed_tokens)} 个过去7天新上线且流动性足够的代币")
            
            # 如果筛选后没有结果，输出一些调试信息
            if len(newly_listed_tokens) == 0 and stats['within_7_days'] > 0:
                print(f"\n⚠️ 警告: 有 {stats['within_7_days']} 个过去7天内的代币，但都被流动性阈值过滤掉了")
                print(f"   当前流动性阈值: {MIN_LIQUIDITY} USD")
                print(f"   建议: 可以尝试降低 MIN_LIQUIDITY 的值")
            
            return newly_listed_tokens
            
        except Exception as e:
            print(f"抓取新上线代币列表失败: {e}")
            import traceback
            traceback.print_exc()
            return []

    def check_price_multiplier(self, mint_address: str, created_at: int) -> Optional[float]:
        """
        检查代币是否在上线后涨到指定倍数以上
        
        Args:
            mint_address: 代币 mint 地址
            created_at: 代币创建时间戳（毫秒）
            
        Returns:
            最高涨幅倍数，如果无法获取则返回 None
        """
        try:
            # 将创建时间转换为秒
            created_at_seconds = created_at // 1000 if created_at > 1e10 else created_at
            now = int(time.time())
            
            # 从创建时间开始获取价格历史
            url = f"https://public-api.birdeye.so/defi/history_price?address={mint_address}&address_type=token&type=1h&time_from={created_at_seconds}&time_to={now}"
            headers = {"X-API-KEY": self.birdeye_api_key, "x-chain": "solana"}
            
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code != 200:
                return None
            
            data = response.json()
            items = data.get('data', {}).get('items', [])
            
            if not items:
                return None
            
            # 找到最低价和最高价
            prices = [item.get('o', 0) for item in items if item.get('o', 0) > 0]  # 开盘价
            prices.extend([item.get('l', 0) for item in items if item.get('l', 0) > 0])  # 最低价
            prices.extend([item.get('h', 0) for item in items if item.get('h', 0) > 0])  # 最高价
            
            if not prices:
                return None
            
            low_price = min(p for p in prices if p > 0)
            high_price = max(p for p in prices if p > 0)
            
            if low_price == 0:
                return None
            
            multiplier = high_price / low_price
            return multiplier
            
        except Exception as e:
            print(f"获取价格分析失败 ({mint_address}): {e}")
            return None
    
    def get_token_transactions(self, mint_address: str, pair_address: str = None, limit: int = 1000) -> List[Dict]:
        """
        通过 Solana RPC 和 Helius API 获取代币的交易记录
        
        Args:
            mint_address: 代币 mint 地址
            pair_address: 交易对地址（可选）
            limit: 最大返回数量
            
        Returns:
            交易记录列表
        """
        try:
            # 方法1: 通过交易对地址获取交易（如果提供了 pair_address）
            if pair_address:
                # 使用 Solana RPC 获取交易签名
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                                "method": "getSignaturesForAddress",
                                "params": [
                                    pair_address,
                                    {"limit": min(limit, 1000)}
                                ]
                            }
                
                response = requests.post(self.rpc_url, json=payload, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    signatures = [sig['signature'] for sig in data.get('result', [])]
                    
                    # 通过 Helius API 获取交易详情
                    if signatures and self.helius_api_key:
                        transactions = []
                        # 批量获取交易详情（每次最多10个）
                        batch_size = 10
                        for i in range(0, len(signatures), batch_size):
                            batch = signatures[i:i+batch_size]
                            url = "https://api.helius.xyz/v0/transactions/"
                            params = {
                                'api-key': self.helius_api_key,
                            }
                            payload_batch = {
                                'transactions': batch
                            }
                            
                            try:
                                resp = requests.post(url, params=params, json=payload_batch, timeout=30)
                                if resp.status_code == 200:
                                    batch_data = resp.json()
                                    if isinstance(batch_data, list):
                                        transactions.extend(batch_data)
                                time.sleep(0.5)  # 防止请求过快
                            except:
                                continue
                        
                        return transactions[:limit]
            
            # 方法2: 通过代币 mint 地址获取交易签名（使用 Solana RPC）
            # 注意：这需要知道代币账户地址，这里简化处理
            # 实际应用中可能需要通过其他方式获取
            
            return []
            
        except Exception as e:
            print(f"获取交易记录失败 ({mint_address}): {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def analyze_trader_profits(self, mint_address: str, transactions: List[Dict]) -> List[Dict]:
        """
        分析交易者在该代币上的盈利情况，找出盈利10倍以上的交易者
        
        Args:
            mint_address: 代币 mint 地址
            transactions: 交易记录列表
            
        Returns:
            Top traders 列表，按盈利倍数排序
        """
        # 按交易者地址分组
        trader_trades = defaultdict(list)
        
        for tx in transactions:
            # 从交易中提取交易者地址
            # Helius API 返回的交易格式可能不同，需要根据实际格式解析
            signer = tx.get('signer')
            if not signer:
                continue
            
            # 解析交易中的代币转移
            token_transfers = tx.get('tokenTransfers', [])
            native_transfers = tx.get('nativeTransfers', [])
            
            # 找到与该代币相关的转移
            sol_in = 0.0
            sol_out = 0.0
            token_in = 0.0
            token_out = 0.0
            
            for transfer in token_transfers:
                if transfer.get('mint') == mint_address:
                    if transfer.get('toUserAccount') == signer:
                        token_in += transfer.get('tokenAmount', 0)
                    elif transfer.get('fromUserAccount') == signer:
                        token_out += transfer.get('tokenAmount', 0)
            
            for transfer in native_transfers:
                if transfer.get('toUserAccount') == signer:
                    sol_in += transfer.get('amount', 0) / 1e9
                elif transfer.get('fromUserAccount') == signer:
                    sol_out += transfer.get('amount', 0) / 1e9
            
            # 记录交易
            if token_in > 0 or token_out > 0:
                trader_trades[signer].append({
                    'timestamp': tx.get('timestamp', 0),
                    'sol_in': sol_in,
                    'sol_out': sol_out,
                    'token_in': token_in,
                    'token_out': token_out,
                })
        
        # 计算每个交易者的盈利
        trader_profits = []
        
        for trader_address, trades in trader_trades.items():
            # 按时间排序
            trades.sort(key=lambda x: x['timestamp'])
            
            # 计算总投入和总产出
            total_sol_invested = sum(t['sol_in'] for t in trades)
            total_sol_received = sum(t['sol_out'] for t in trades)
            
            if total_sol_invested == 0:
                continue
            
            # 计算盈利倍数
            profit_multiplier = total_sol_received / total_sol_invested if total_sol_invested > 0 else 0
            
            if profit_multiplier >= MIN_PROFIT_MULTIPLIER:
                trader_profits.append({
                    'address': trader_address,
                    'profit_multiplier': profit_multiplier,
                    'total_invested_sol': total_sol_invested,
                    'total_received_sol': total_sol_received,
                    'trade_count': len(trades),
                })
        
        # 按盈利倍数排序
        trader_profits.sort(key=lambda x: x['profit_multiplier'], reverse=True)
        
        return trader_profits[:TOP_TRADERS_LIMIT]
    
    def get_top_traders_for_token(self, mint_address: str, pair_address: str = None) -> List[Dict]:
        """
        获取指定代币的 top traders 钱包地址
        
        Args:
            mint_address: 代币 mint 地址
            pair_address: 交易对地址（可选）
            
        Returns:
            Top traders 列表
        """
        print(f"正在分析代币 {mint_address} 的交易者...")
        
        # 获取交易记录
        transactions = self.get_token_transactions(mint_address, pair_address)
        
        if not transactions:
            print(f"未找到代币 {mint_address} 的交易记录")
            return []
        
        print(f"获取到 {len(transactions)} 条交易记录")
        
        # 分析交易者盈利
        top_traders = self.analyze_trader_profits(mint_address, transactions)
        
        return top_traders


def main():
    """
    主函数：执行完整的分析流程
    """
    analyzer = TokenAnalyzer()
    
    # 1. 获取过去7天新上线的代币
    newly_listed_tokens = analyzer.get_newly_listed_tokens()
    
    if not newly_listed_tokens:
        print("未找到新上线的代币")
        return
    
    print(f"\n开始分析 {len(newly_listed_tokens)} 个新上线代币...")
    
    results = []
    
    for token_info in newly_listed_tokens:
        mint_address = token_info['mint']
        if not mint_address:
            continue
        
        print(f"\n分析代币: {token_info.get('symbol', 'Unknown')} ({mint_address})")
        
        # 2. 检查价格涨幅
        multiplier = analyzer.check_price_multiplier(mint_address, token_info['created_at'])
        
        if not multiplier or multiplier < MULTIPLIER:
            print(f"  涨幅未达到 {MULTIPLIER} 倍 (当前: {multiplier:.2f}x)")
            time.sleep(1)
            continue
        
        print(f"  ✅ 涨幅达到 {multiplier:.2f} 倍！")
        
        # 3. 获取 top traders
        top_traders = analyzer.get_top_traders_for_token(mint_address, token_info.get('pair_address'))
        
        if top_traders:
            print(f"  🎯 找到 {len(top_traders)} 个盈利 {MIN_PROFIT_MULTIPLIER} 倍以上的交易者:")
            for i, trader in enumerate(top_traders, 1):
                print(f"    {i}. {trader['address']} - 盈利 {trader['profit_multiplier']:.2f}x "
                      f"(投入: {trader['total_invested_sol']:.4f} SOL, "
                      f"获得: {trader['total_received_sol']:.4f} SOL)")
            
            results.append({
                'token': token_info,
                'price_multiplier': multiplier,
                'top_traders': top_traders,
            })
        else:
            print(f"  未找到盈利 {MIN_PROFIT_MULTIPLIER} 倍以上的交易者")
        
        time.sleep(2)  # 防止请求过快
    
    # 4. 输出结果
    print("\n" + "="*80)
    print("分析结果汇总:")
    print("="*80)
    
    for result in results:
        token = result['token']
        print(f"\n代币: {token.get('symbol', 'Unknown')} ({token['mint']})")
        print(f"涨幅: {result['price_multiplier']:.2f}x")
        print(f"Top Traders ({len(result['top_traders'])} 个):")
        
        for trader in result['top_traders']:
            print(f"  - {trader['address']} (盈利 {trader['profit_multiplier']:.2f}x)")
        
        print(f"链接: https://dexscreener.com/solana/{token['pair_address']}")
    
    # 5. 保存结果到文件
    if not os.path.exists("results"):
        os.mkdir("results")
    output_file = f"results/top_traders_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print(f"\n结果已保存到: {output_file}")


if __name__ == "__main__":
    main()
