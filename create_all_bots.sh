#!/bin/bash
# 启动所有Bot进程
# 使用方法: ./create_all_bots.sh
# 注意: 使用完整路径启动，方便在ps命令中区分不同的bot

cd /root/project/botA_tugou && nohup python /root/project/botA_tugou/main.py > A.out 2>&1 &
cd /root/project/botB_stable && nohup python /root/project/botB_stable/main.py > B.out 2>&1 &
cd /root/project/botC_diamond && nohup python /root/project/botC_diamond/main.py > C.out 2>&1 &

echo "✅ 所有Bot进程已启动"
echo "📋 查看进程: ps -ef | grep 'python.*main.py'"
echo "📋 查看日志: tail -f /root/project/bot*/A.out /root/project/bot*/B.out /root/project/bot*/C.out"
