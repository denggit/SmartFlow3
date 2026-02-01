#!/bin/bash
# 启动Bot监控守护程序
# 该脚本会启动monitor_bots.py来监控所有bot进程

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITOR_SCRIPT="$SCRIPT_DIR/monitor_bots.py"

# 检查monitor_bots.py是否存在
if [ ! -f "$MONITOR_SCRIPT" ]; then
    echo "❌ 错误: monitor_bots.py 不存在: $MONITOR_SCRIPT"
    exit 1
fi

# 检查是否已经有监控进程在运行
if pgrep -f "monitor_bots.py" > /dev/null; then
    echo "⚠️  警告: 监控程序已经在运行中"
    echo "   如果需要重启，请先停止现有进程: pkill -f monitor_bots.py"
    exit 1
fi

# 启动监控程序
echo "🚀 启动Bot监控守护程序..."
nohup python "$MONITOR_SCRIPT" > "$SCRIPT_DIR/log/monitor_startup.log" 2>&1 &

# 等待一下确认启动
sleep 2

# 检查是否成功启动
if pgrep -f "monitor_bots.py" > /dev/null; then
    echo "✅ 监控程序启动成功"
    echo "📋 查看日志: tail -f $SCRIPT_DIR/log/monitor_*.log"
    echo "🛑 停止监控: pkill -f monitor_bots.py"
else
    echo "❌ 监控程序启动失败，请检查日志: $SCRIPT_DIR/log/monitor_startup.log"
    exit 1
fi
