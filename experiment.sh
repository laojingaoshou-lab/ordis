#!/bin/bash
# Ordis 自愈实验：一键演示「故障 → 自动修复 → AI 诊断 → 技能入库」
# 用法: bash /opt/ordis/experiment.sh
PORT=3971

echo "=== [1/4] 注入故障: 启动测试服务后杀掉 ==="
pkill -f "http.server $PORT" 2>/dev/null || true
cd /tmp && setsid python3 -m http.server $PORT >/dev/null 2>&1 &
sleep 2
ss -tln | grep -q ":$PORT " && echo "服务已启动 (:$PORT)"
pkill -f "http.server $PORT"
sleep 1
ss -tln | grep -q ":$PORT " && echo "还在?!" || echo "故障已注入: :$PORT 已死"

echo ""
echo "=== [2/4] 触发一轮检测 (ordis once) ==="
cd /opt/ordis
timeout 60 ordis once 2>&1 | tail -3 || true

echo ""
echo "=== [3/4] 等 AI 诊断落盘 (~30s) ==="
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 5
  if grep -q "$PORT" ordis/logs/cases.json 2>/dev/null; then
    echo "案例已生成 (等待 ${i}x5s)"
    break
  fi
done

echo ""
echo "=== [4/4] 结果查看 ==="
echo "--- 技能库:"
ordis skills
echo ""
echo "--- 若显示待确认技能，执行生效:"
echo "    ordis skills confirm <上面显示的 skill_id>"
echo "--- 生效后再注入一次同故障，将看到秒级自动修复:"
echo "    cd /tmp && setsid python3 -m http.server $PORT >/dev/null 2>&1 & sleep 2; pkill -f 'http.server \$PORT'; sleep 40; ss -tln | grep $PORT"
