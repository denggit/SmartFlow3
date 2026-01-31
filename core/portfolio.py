#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : core/portfolio.py
@Description: 核心资产管理 (支持断电记忆/持久化保存 + 每日盈亏统计)
"""
import asyncio
import json
import os
import aiohttp
from datetime import datetime, timedelta  # 🔥 新增 timedelta

# 导入配置和工具
from config.settings import TARGET_WALLET, SLIPPAGE_SELL, TAKE_PROFIT_ROI
from services.notification import send_email_async
from utils.logger import logger

# 数据文件路径
DATA_DIR = "data"
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")


class PortfolioManager:
    def __init__(self, trader):
        self.trader = trader
        self.portfolio = {}  # 当前持仓
        self.trade_history = []  # 历史记录
        self.buy_counts_cache = {}  # 买入次数缓存
        self.is_running = True

        # 🔥 初始化时，加载硬盘上的数据
        self._ensure_data_dir()
        self._load_data()
        self._rebuild_buy_counts_cache()

    def _ensure_data_dir(self):
        """ 确保 data 目录存在 """
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)

    def _load_data(self):
        """ 从硬盘加载数据 (恢复记忆) """
        # 1. 加载持仓
        if os.path.exists(PORTFOLIO_FILE):
            try:
                with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f:
                    self.portfolio = json.load(f)
                logger.info(f"📂 已恢复持仓记忆: {len(self.portfolio)} 个代币")
            except Exception as e:
                logger.error(f"❌ 读取持仓文件失败: {e}")

        # 2. 加载历史
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    self.trade_history = json.load(f)
            except Exception:
                pass

    def _rebuild_buy_counts_cache(self):
        """ 🚀 启动加速：预先统计历史买入次数 """
        self.buy_counts_cache = {}
        for record in self.trade_history:
            if record.get('action') == 'BUY':
                token = record.get('token')
                if token:
                    self.buy_counts_cache[token] = self.buy_counts_cache.get(token, 0) + 1
        logger.info(f"⚡️ 交易历史缓存已重建，包含 {len(self.buy_counts_cache)} 个代币记录")

    def _save_portfolio(self):
        """ 保存持仓到硬盘 """
        try:
            with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.portfolio, f, indent=4)
        except Exception as e:
            logger.error(f"❌ 保存持仓失败: {e}")

    def _save_history(self):
        """ 保存历史到硬盘 """
        try:
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.trade_history, f, indent=4)
        except Exception as e:
            logger.error(f"❌ 保存历史失败: {e}")

    def _record_history(self, action, token, amount, value_sol):
        """ 记录历史并保存 """
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "token": token,
            "amount": amount,
            "value_sol": value_sol
        }
        self.trade_history.append(record)
        self._save_history()  # 立即保存

    def add_position(self, token_mint, amount_bought, cost_sol):
        if token_mint not in self.portfolio:
            self.portfolio[token_mint] = {'my_balance': 0, 'cost_sol': 0}

        self.portfolio[token_mint]['my_balance'] += amount_bought
        self.portfolio[token_mint]['cost_sol'] += cost_sol

        # 更新缓存
        self.buy_counts_cache[token_mint] = self.buy_counts_cache.get(token_mint, 0) + 1

        # 🔥 立即保存到硬盘
        self._save_portfolio()

        self._record_history("BUY", token_mint, amount_bought, cost_sol)
        logger.info(f"📝 [记账] 新增持仓 {token_mint[:6]}... | 数量: {self.portfolio[token_mint]['my_balance']}")

    def get_buy_counts(self, token_mint):
        """ 查询历史买入次数 (O(1)) """
        return self.buy_counts_cache.get(token_mint, 0)

    async def execute_proportional_sell(self, token_mint, smart_money_sold_amt):
        # 1. 检查持仓
        if token_mint not in self.portfolio or self.portfolio[token_mint]['my_balance'] <= 0:
            return

        logger.info(f"👀 监测到大佬卖出 {token_mint[:6]}... 正在计算比例...")

        # 2. 查询大佬剩余持仓
        smart_money_remaining = await self.trader.get_token_balance(TARGET_WALLET, token_mint)
        total_before_sell = smart_money_sold_amt + smart_money_remaining

        sell_ratio = 1.0
        if total_before_sell > 0:
            sell_ratio = smart_money_sold_amt / total_before_sell
            if sell_ratio > 0.99: sell_ratio = 1.0

        my_holdings = self.portfolio[token_mint]['my_balance']
        amount_to_sell = int(my_holdings * sell_ratio)

        if amount_to_sell < 100: return

        # 3. 执行卖出
        logger.info(f"📉 跟随卖出: {amount_to_sell} (占持仓 {sell_ratio:.2%})")
        success, est_sol_out = await self.trader.execute_swap(
            input_mint=token_mint,
            output_mint=self.trader.SOL_MINT,
            amount_lamports=amount_to_sell,
            slippage_bps=SLIPPAGE_SELL
        )

        if success:
            self.portfolio[token_mint]['my_balance'] -= amount_to_sell

            # 🔥 更新并保存
            if self.portfolio[token_mint]['my_balance'] < 100:
                del self.portfolio[token_mint]
                logger.info(f"✅ {token_mint[:6]}... 已清仓完毕")

                # 尝试关闭账户回血
                logger.info(f"🧹 正在尝试回收账户租金...")
                await asyncio.sleep(2)
                asyncio.create_task(self.trader.close_token_account(token_mint))

            self._save_portfolio()  # 保存
            self._record_history("SELL", token_mint, amount_to_sell, est_sol_out)

            # 邮件通知
            msg = f"检测到聪明钱卖出，已跟随卖出。\n\n代币: {token_mint}\n数量: {amount_to_sell}\n比例: {sell_ratio:.1%}"
            asyncio.create_task(send_email_async(f"📉 跟随卖出成功: {token_mint[:6]}...", msg))

    async def monitor_sync_positions(self):
        """ 防断网兜底：检查链上状态与粉尘过滤 """
        logger.info("🛡️ 持仓同步防断网线程已启动 (每20秒检查一次)...")

        async with aiohttp.ClientSession(trust_env=False) as session:
            while self.is_running:
                if not self.portfolio:
                    await asyncio.sleep(5)
                    continue

                for token_mint in list(self.portfolio.keys()):
                    try:
                        my_data = self.portfolio[token_mint]
                        if my_data['my_balance'] <= 0: continue

                        # 获取大佬的原始余额
                        sm_amount_raw = await self.trader.get_token_balance_raw(TARGET_WALLET, token_mint)
                        should_sell = False
                        reason = ""

                        if sm_amount_raw == 0:
                            should_sell = True
                            reason = "大佬余额为 0"
                        else:
                            # 价值检查 (Value Check)
                            quote = await self.trader.get_quote(session, token_mint, self.trader.SOL_MINT,
                                                                sm_amount_raw)
                            if quote:
                                val_in_sol = int(quote['outAmount']) / 10 ** 9
                                if val_in_sol < 0.05:
                                    should_sell = True
                                    reason = f"大佬余额价值仅 {val_in_sol:.4f} SOL (判定为粉尘)"

                        if should_sell:
                            logger.warning(f"😱 发现异常！持有 {token_mint[:6]}... | 原因: {reason}")
                            logger.warning(f"🛡️ 触发防断网机制：立即强制清仓！")
                            await self.force_sell_all(token_mint, my_data['my_balance'], -0.99)

                    except Exception as e:
                        logger.error(f"同步检查异常: {e}")

                await asyncio.sleep(20)

    async def monitor_1000x_profit(self):
        """ 止盈监控 """
        logger.info("💰 收益监控线程已启动...")
        async with aiohttp.ClientSession(trust_env=False) as session:
            while self.is_running:
                if not self.portfolio:
                    await asyncio.sleep(5)
                    continue
                for token_mint in list(self.portfolio.keys()):
                    try:
                        data = self.portfolio[token_mint]
                        if data['my_balance'] <= 0: continue

                        # 询价
                        quote = await self.trader.get_quote(session, token_mint, self.trader.SOL_MINT,
                                                            data['my_balance'])
                        if quote:
                            curr_val = int(quote['outAmount'])
                            cost = data['cost_sol']
                            roi = (curr_val / cost) - 1 if cost > 0 else 0

                            if roi >= TAKE_PROFIT_ROI:
                                logger.warning(f"🚀 触发 {roi * 100:.0f}% 止盈！{token_mint} 强平！")
                                await self.force_sell_all(token_mint, data['my_balance'], roi)
                    except Exception as e:
                        logger.error(f"盯盘异常: {e}")
                await asyncio.sleep(10)

    async def force_sell_all(self, token_mint, amount, roi):
        """ 强制卖出辅助函数 """
        success, est_sol_out = await self.trader.execute_swap(
            token_mint, self.trader.SOL_MINT, amount, SLIPPAGE_SELL
        )
        if success:
            if token_mint in self.portfolio:
                del self.portfolio[token_mint]

            logger.info(f"🧹 [强平] 正在尝试回收账户租金...")
            await asyncio.sleep(2)
            asyncio.create_task(self.trader.close_token_account(token_mint))

            self._save_portfolio()
            self._record_history("SELL_FORCE", token_mint, amount, est_sol_out)

            if roi == -0.99:
                subject = f"🛡️ 防断网风控: {token_mint[:6]}..."
                msg = f"检测到聪明钱已清仓，已补救卖出。\n\n代币: {token_mint}"
            else:
                subject = f"🚀 暴富止盈: {token_mint[:6]}..."
                msg = f"触发 1000% 止盈！\n\n代币: {token_mint}\n收益率: {roi * 100:.1f}%\n动作: 全仓卖出"

            asyncio.create_task(send_email_async(subject, msg))

    async def schedule_daily_report(self):
        """ 每日日报调度器 """
        logger.info("📅 日报调度器已启动 (每天 09:00 发送)...")
        while self.is_running:
            now = datetime.now()
            target_time = now.replace(hour=9, minute=0, second=0, microsecond=0)

            if now >= target_time:
                target_time += timedelta(days=1)

            sleep_seconds = (target_time - now).total_seconds()
            logger.info(f"⏳ 距离发送日报还有 {sleep_seconds / 3600:.1f} 小时")

            await asyncio.sleep(sleep_seconds)
            await self.send_daily_summary()
            await asyncio.sleep(60)

    async def send_daily_summary(self):
        """ 生成并发送日报 (含当日盈亏统计) """
        logger.info("📊 正在生成每日日报...")

        async with aiohttp.ClientSession(trust_env=True) as session:
            try:
                # 1. 基础数据获取 (SOL价格 & 余额)
                usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                quote = await self.trader.get_quote(session, self.trader.SOL_MINT, usdc_mint, 1 * 10 ** 9)
                sol_price = float(quote['outAmount']) / 10 ** 6 if quote else 0

                balance_resp = await self.trader.rpc_client.get_balance(self.trader.payer.pubkey())
                sol_balance = balance_resp.value / 10 ** 9

                # 2. 计算当前持仓价值
                holdings_val_sol = 0
                holdings_details = ""
                if self.portfolio:
                    for mint, data in self.portfolio.items():
                        qty = data['my_balance']
                        if qty > 0:
                            q = await self.trader.get_quote(session, mint, self.trader.SOL_MINT, qty)
                            val = int(q['outAmount']) / 10 ** 9 if q else 0
                            holdings_val_sol += val
                            holdings_details += f"- {mint[:6]}...: 持有 {qty}, 价值 {val:.2f} SOL\n"

                total_asset_sol = sol_balance + holdings_val_sol
                total_asset_usd = total_asset_sol * sol_price

                # --- 🔥🔥 核心新增：当日盈亏回放计算 (PnL Replay) 🔥🔥 ---
                # 定义“今天” (过去24小时)
                yesterday = datetime.now() - timedelta(days=1)

                # 临时变量用于回放计算
                temp_holdings = {}  # {token: amount}
                temp_costs = {}  # {token: total_cost_sol}

                daily_profit_sol = 0.0
                daily_wins = 0
                daily_losses = 0
                daily_trade_count = 0

                # 遍历所有历史，重建成本并统计今日表现
                for record in self.trade_history:
                    token = record['token']
                    action = record['action']
                    amount = record['amount']
                    val = record['value_sol']

                    try:
                        rec_time = datetime.strptime(record['time'], "%Y-%m-%d %H:%M:%S")
                    except:
                        continue  # 跳过时间格式错误的旧数据

                    if action == 'BUY':
                        # 累加持仓和成本
                        temp_holdings[token] = temp_holdings.get(token, 0) + amount
                        temp_costs[token] = temp_costs.get(token, 0.0) + val

                    elif 'SELL' in action:
                        # 计算本次卖出的成本 (平均成本法)
                        current_holding = temp_holdings.get(token, 0)
                        total_cost = temp_costs.get(token, 0.0)

                        if current_holding > 0:
                            avg_price = total_cost / current_holding
                            cost_of_this_sell = avg_price * amount

                            # 计算单笔盈亏
                            pnl = val - cost_of_this_sell

                            # 更新剩余持仓和成本
                            temp_holdings[token] = max(0, current_holding - amount)
                            temp_costs[token] = max(0.0, total_cost - cost_of_this_sell)

                            # 统计：如果这笔卖出发生在今天
                            if rec_time >= yesterday:
                                daily_profit_sol += pnl
                                daily_trade_count += 1
                                if pnl > 0:
                                    daily_wins += 1
                                else:
                                    daily_losses += 1

                # 计算胜率
                daily_win_rate = (daily_wins / daily_trade_count * 100) if daily_trade_count > 0 else 0.0
                daily_profit_usd = daily_profit_sol * sol_price

                # 4. 生成报告文本
                report = f"""
【📅 每日交易与资产报告】
时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

💰 资产概览:
-------------------
• SOL 价格: ${sol_price:.2f}
• 钱包余额: {sol_balance:.4f} SOL
• 持仓价值: {holdings_val_sol:.4f} SOL
• 总计资产: {total_asset_sol:.4f} SOL (≈ ${total_asset_usd:.2f})

📈 今日战绩 (近24h):
-------------------
• 已结盈亏: {'+' if daily_profit_sol >= 0 else ''}{daily_profit_sol:.4f} SOL (≈ ${daily_profit_usd:.2f})
• 今日胜率: {daily_win_rate:.1f}% ({daily_wins} 胜 / {daily_losses} 负)
• 交易笔数: {daily_trade_count} 笔

📊 历史累计统计:
-------------------
• 总买入次数: {sum(1 for x in self.trade_history if x['action'] == 'BUY')}
• 总卖出次数: {sum(1 for x in self.trade_history if 'SELL' in x['action'])}

👜 当前持仓明细:
{holdings_details if holdings_details else "(空仓)"}

🤖 机器人状态: 正常运行中
"""
                await send_email_async("📊 [日报] 资产与盈亏统计", report, attachment_path=PORTFOLIO_FILE)
                logger.info("✅ 日报已发送 (包含详细盈亏数据)")

            except Exception as e:
                logger.error(f"生成日报失败: {e}")