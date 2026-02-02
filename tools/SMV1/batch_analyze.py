#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : batch_analyze.py
@Description: 批量钱包分析工具 (V5 优化版)
              - 批量分析多个钱包地址
              - 自动黑名单过滤低质量钱包
              - 导出 Excel 报告
              - 改进错误处理和日志记录
@Author     : Auto-generated
@Date       : 2026-02-01
"""
import asyncio
import logging
import os
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

import aiohttp
import pandas as pd
from tqdm.asyncio import tqdm

# 确保能找到 analyze_wallet 模块
current_dir = Path(__file__).parent
sys.path.insert(0, str(current_dir))

try:
    from analyze_wallet import WalletAnalyzer, get_detailed_scores
except ImportError:
    print("❌ 错误：找不到 analyze_wallet 模块")
    sys.exit(1)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === ⚙️ 配置常量 ===
# 文件路径：指向 tools 目录（父目录）
TOOLS_DIR = Path(__file__).parent.parent
TRASH_FILE = str(TOOLS_DIR / "wallets_trash.txt")
WALLETS_FILE = str(TOOLS_DIR / "wallets_check.txt")
RESULTS_DIR = str(TOOLS_DIR / "results")
MIN_SCORE_THRESHOLD_1 = 45  # 评分阈值1：低于此值且代币数>=10时加入黑名单
MIN_SCORE_THRESHOLD_2 = 20  # 评分阈值2：低于此值直接加入黑名单
CONCURRENT_LIMIT = 1  # 并发限制


def is_valid_solana_address(address: str) -> bool:
    """
    验证是否为有效的 Solana 钱包地址
    
    Args:
        address: 待验证的地址字符串
        
    Returns:
        是否为有效的 Solana 地址
    """
    if not address or not isinstance(address, str):
        return False
    
    # Solana 地址长度通常在 32-44 位，使用 Base58 字符集
    if not (32 <= len(address) <= 44):
        return False
    
    # Base58 字符集：不包含 0, O, I, l
    if not re.match(r'^[1-9A-HJ-NP-Za-km-z]+$', address):
        return False
    
    # 排除系统地址
    if address == "So11111111111111111111111111111111111111111":
        return False
    
    return True


class WalletListSaver:
    """
    钱包列表保存器：负责将有效的钱包地址保存回文件
    """
    
    @staticmethod
    def save_valid_addresses(
        addresses: List[str],
        wallets_file: str = WALLETS_FILE
    ) -> bool:
        """
        保存有效的钱包地址到文件（去重、验证格式）
        
        Args:
            addresses: 钱包地址列表
            wallets_file: 钱包列表文件路径
            
        Returns:
            是否成功保存
        """
        if not addresses:
            logger.warning("没有地址需要保存")
            return False
        
        try:
            # 验证并去重
            valid_addresses = set()
            for addr in addresses:
                addr = addr.strip()
                if addr and is_valid_solana_address(addr):
                    valid_addresses.add(addr)
            
            if not valid_addresses:
                logger.warning("没有有效的钱包地址需要保存")
                return False
            
            # 排序并保存
            sorted_addresses = sorted(list(valid_addresses))
            
            with open(wallets_file, 'w', encoding='utf-8') as f:
                for addr in sorted_addresses:
                    f.write(f"{addr}\n")
            
            logger.info(f"已保存 {len(sorted_addresses)} 个有效钱包地址到 {wallets_file}")
            return True
            
        except Exception as e:
            logger.error(f"保存钱包地址失败: {e}")
            return False


class TrashListManager:
    """
    黑名单管理器：负责管理低质量钱包黑名单
    
    职责：
    - 加载黑名单
    - 添加地址到黑名单
    - 检查地址是否在黑名单中
    """
    
    def __init__(self, trash_file: str = TRASH_FILE):
        """
        初始化黑名单管理器
        
        Args:
            trash_file: 黑名单文件路径
        """
        self.trash_file = trash_file
        self._trash_set: Optional[Set[str]] = None
    
    def load(self) -> Set[str]:
        """
        加载黑名单
        
        Returns:
            黑名单地址集合
        """
        if self._trash_set is not None:
            return self._trash_set
        
        if not os.path.exists(self.trash_file):
            self._trash_set = set()
            return self._trash_set
        
        try:
            with open(self.trash_file, 'r', encoding='utf-8') as f:
                self._trash_set = {line.strip() for line in f if line.strip()}
            logger.info(f"加载黑名单: {len(self._trash_set)} 个地址")
        except Exception as e:
            logger.error(f"加载黑名单失败: {e}")
            self._trash_set = set()
        
        return self._trash_set
    
    def add(self, address: str) -> bool:
        """
        添加地址到黑名单
        
        Args:
            address: 钱包地址
            
        Returns:
            是否成功添加
        """
        try:
            with open(self.trash_file, 'a', encoding='utf-8') as f:
                f.write(f"{address}\n")
            
            if self._trash_set is not None:
                self._trash_set.add(address)
            
            logger.debug(f"已添加地址到黑名单: {address[:6]}...")
            return True
        except Exception as e:
            logger.error(f"添加黑名单失败: {e}")
            return False
    
    def contains(self, address: str) -> bool:
        """
        检查地址是否在黑名单中
        
        Args:
            address: 钱包地址
            
        Returns:
            是否在黑名单中
        """
        if self._trash_set is None:
            self.load()
        return address in (self._trash_set or set())


class WalletListLoader:
    """
    钱包列表加载器：负责从文件加载钱包地址列表
    """
    
    @staticmethod
    def load(wallets_file: str = WALLETS_FILE) -> List[str]:
        """
        从文件加载钱包地址列表
        
        Args:
            wallets_file: 钱包列表文件路径
            
        Returns:
            钱包地址列表
        """
        if not os.path.exists(wallets_file):
            logger.error(f"找不到钱包列表文件: {wallets_file}")
            return []
        
        try:
            with open(wallets_file, 'r', encoding='utf-8') as f:
                addresses = [
                    line.strip()
                    for line in f
                    if line.strip() and not line.startswith("#")
                ]
                addresses = list(set(addresses))
            logger.info(f"从 {wallets_file} 加载了 {len(addresses)} 个地址")
            return addresses
        except Exception as e:
            logger.error(f"加载钱包列表失败: {e}")
            return []


class BatchAnalyzer:
    """
    批量分析器：负责批量分析多个钱包
    
    职责：
    - 并发分析多个钱包
    - 自动过滤低质量钱包
    - 生成分析报告
    """
    
    def __init__(
        self,
        analyzer: WalletAnalyzer,
        trash_manager: TrashListManager,
        concurrent_limit: int = CONCURRENT_LIMIT
    ):
        """
        初始化批量分析器
        
        Args:
            analyzer: 钱包分析器实例
            trash_manager: 黑名单管理器实例
            concurrent_limit: 并发限制
        """
        self.analyzer = analyzer
        self.trash_manager = trash_manager
        self.concurrent_limit = concurrent_limit
        self.semaphore = asyncio.Semaphore(concurrent_limit)
    
    async def analyze_one_wallet(
        self,
        session: aiohttp.ClientSession,
        address: str,
        pbar: tqdm,
        max_txs: int = 5000
    ) -> Optional[Dict]:
        """
        分析单个钱包
        
        Args:
            session: aiohttp 会话对象
            address: 钱包地址
            pbar: 进度条对象
            max_txs: 最大交易数量
            
        Returns:
            分析结果字典，如果失败或应过滤则返回 None
        """
        try:
            # 1. 拉取交易数据
            txs = await self.analyzer.fetch_history_pagination(session, address, max_txs)
            if not txs:
                pbar.update(1)
                return None
            
            # 2. 解析代币项目
            results = await self.analyzer.parse_token_projects(session, txs, address)
            if not results:
                pbar.update(1)
                return None
            
            # 3. 计算评分
            score, tier, desc, radar = get_detailed_scores(results)
            
            # 4. 自动黑名单过滤
            if score < MIN_SCORE_THRESHOLD_1 and len(results) >= 10:
                self.trash_manager.add(address)
                pbar.update(1)
                return None
            elif score < MIN_SCORE_THRESHOLD_2 and len(results) >= 5:
                self.trash_manager.add(address)
                pbar.update(1)
                return None
            
            # 5. 提取最佳定位
            best_role = "未知"
            if radar:
                best_role = max(radar, key=radar.get)
            
            # 6. 计算基础指标
            wins = [r for r in results if r.get('is_win', False)]
            win_rate = len(wins) / len(results) if results else 0
            total_profit = sum(r.get('profit', 0) for r in results)
            max_roi = max([r.get('roi', 0) for r in results]) if results else 0
            hold_times = [r.get('hold_time', 0) for r in results if r.get('hold_time', 0) > 0]
            median_hold = statistics.median(hold_times) if hold_times else 0
            
            # 提取置信度
            confidence = "高" if len(results) > 10 else "低"
            
            # 解析盈亏比
            profit_factor = 0.0
            try:
                profit_factor_str = desc.split("|")[0].split(":")[-1].strip()
                profit_factor = float(profit_factor_str)
            except (ValueError, IndexError):
                logger.warning(f"无法解析盈亏比: {desc}")
            
            pbar.update(1)
            return {
                "钱包地址": address,
                "综合评分": score,
                "战力评级": tier,
                "置信度": confidence,
                "最佳定位": best_role,
                "盈亏比": profit_factor,
                "总盈亏(SOL)": round(total_profit, 2),
                "胜率": win_rate,
                "最大单笔ROI": f"{max_roi:.0%}",
                "中位持仓(分)": round(median_hold, 1),
                "代币数": len(results),
                "分析时间": datetime.now().strftime("%Y-%m-%d %H:%M")
            }
            
        except Exception as e:
            logger.error(f"分析钱包 {address[:6]}... 时出错: {e}")
            pbar.update(1)
            return None
    
    async def analyze_batch(
        self,
        addresses: List[str],
        max_txs: int = 5000
    ) -> List[Dict]:
        """
        批量分析钱包列表
        
        Args:
            addresses: 钱包地址列表
            max_txs: 每个钱包最大交易数量
            
        Returns:
            分析结果列表
        """
        pbar = tqdm(total=len(addresses), desc="📊 审计进度", unit="钱包", colour="green")
        
        async def sem_task(session, addr):
            async with self.semaphore:
                return await self.analyze_one_wallet(session, addr, pbar, max_txs)
        
        async with aiohttp.ClientSession() as session:
            tasks = [sem_task(session, addr) for addr in addresses]
            raw_results = await asyncio.gather(*tasks)
            results = [r for r in raw_results if r is not None]
        
        pbar.close()
        return results


class ReportExporter:
    """
    报告导出器：负责导出分析结果到 Excel
    """
    
    @staticmethod
    def export(results: List[Dict], output_dir: str = RESULTS_DIR) -> Optional[str]:
        """
        导出分析结果到 Excel
        
        Args:
            results: 分析结果列表
            output_dir: 输出目录
            
        Returns:
            输出文件路径，如果失败则返回 None
        """
        if not results:
            logger.warning("没有结果可导出")
            return None
        
        # 创建输出目录
        os.makedirs(output_dir, exist_ok=True)
        
        try:
            df = pd.DataFrame(results).sort_values(by="综合评分", ascending=False)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = os.path.join(output_dir, f"wallet_ranking_v5_{timestamp}.xlsx")
            df.to_excel(output_file, index=False, engine='openpyxl')
            logger.info(f"导出成功: {output_file} ({len(results)} 条记录)")
            return output_file
        except Exception as e:
            logger.error(f"导出失败: {e}")
            return None


async def main():
    """主函数：批量分析入口"""
    # 初始化组件
    analyzer = WalletAnalyzer()
    trash_manager = TrashListManager()
    batch_analyzer = BatchAnalyzer(analyzer, trash_manager, CONCURRENT_LIMIT)
    exporter = ReportExporter()
    
    # 加载钱包列表和黑名单
    trash_set = trash_manager.load()
    all_addresses = WalletListLoader.load()
    
    if not all_addresses:
        print("❌ 未找到钱包地址列表")
        return
    
    # 过滤黑名单
    addresses = [a for a in all_addresses if not trash_manager.contains(a)]
    skip_count = len(all_addresses) - len(addresses)
    
    if not addresses:
        print(f"🚫 库中所有地址都在黑名单内，或没有新地址。")
        return
    
    print(f"🚀 启动批量分析 V5 | 任务数: {len(addresses)} (跳过黑名单: {skip_count})")
    
    # 执行批量分析
    results = await batch_analyzer.analyze_batch(addresses)
    
    # 导出结果
    if results:
        output_file = exporter.export(results)
        if output_file:
            print(f"\n✅ 导出成功: {output_file}")
        else:
            print("\n⚠️ 导出失败")
    else:
        print("\n🏁 分析结果为空，请检查报错或地址列表。")
    
    # 收集所有有效的钱包地址（从分析结果和原始列表中提取）
    valid_addresses = set()
    
    # 1. 从分析结果中提取（这些是成功分析的钱包）
    if results:
        for r in results:
            addr = r.get('钱包地址', '').strip()
            if addr and is_valid_solana_address(addr):
                valid_addresses.add(addr)
    
    # 2. 从原始列表中提取（包括未分析但格式正确的地址）
    for addr in all_addresses:
        addr = addr.strip()
        if addr and is_valid_solana_address(addr):
            valid_addresses.add(addr)
    
    # 3. 保存有效的钱包地址回文件
    if valid_addresses:
        saved = WalletListSaver.save_valid_addresses(list(valid_addresses), WALLETS_FILE)
        if saved:
            print(f"\n✅ 已过滤并保存 {len(valid_addresses)} 个有效钱包地址到 {WALLETS_FILE}")
        else:
            print(f"\n⚠️ 保存钱包地址失败")
    else:
        print(f"\n⚠️ 未找到有效的钱包地址")


if __name__ == "__main__":
    asyncio.run(main())
