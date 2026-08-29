"""验证权限分级: view 拦截写操作技能 / operate 分级 / root 不限。"""
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, ".")
import ai_diagnose
import promotion
import config
import ai_levels

tmp = Path(tempfile.mkdtemp())
import db
db.DB_PATH = tmp / "test.db"
config.CFG_DIR = tmp

# set_level 会写 model.json——必须劫持到临时目录，否则污染真实 ~/.ordis 配置
import model_config as mc
mc.CONFIG_PATH = tmp / "model.json"

# view 级: 写操作技能拒绝生成
ai_levels.set_level("view")
fake = {"fingerprint": {"type": "x"}, "root_cause": "r",
        "fix_direction": "执行 `systemctl restart myapp` 拉起服务",
        "confidence": 0.85}
promotion.auto_draft_from_ai("process:权限测试", {
    "diagnosis": fake,
})
auto = [s for s in promotion.load_skills() if s.get("source") == "ai_auto"]
assert not auto, "view级不应生成写操作技能"
blocked = [d for d in promotion.load() if d["status"] == "blocked_level"]
assert blocked
print("5. view级拦截写操作技能 OK:", blocked[0]["block_reason"][:50])

# operate 级: restart 放行, apt install 拦截
ai_levels.set_level("operate")
ok1, _ = ai_levels.check_allowed("systemctl restart nginx", "operate")
ok2, r2 = ai_levels.check_allowed("apt install htop", "operate")
assert ok1 and not ok2
print("6. operate级: restart放行 / apt install拦截 OK")

# root 级: 危险命令字符串仅作字符串校验（不执行）
dangerous = "dd if=/dev/zero of=/dev/sdX"
ok3, _ = ai_levels.check_allowed(dangerous, "root")
assert ok3
print("7. root级不限制(纯字符串校验,未执行) OK")
