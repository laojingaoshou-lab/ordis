"""验证: AI 自动修复成功 → 生成待审批技能 → confirm 生效 全流程。"""
import sys
import json
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, ".")

# 先隔离数据库
import db
tmp = Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "test.db"

import ai_diagnose
import promotion
import config

config.CFG_DIR = tmp

# 隔离 AI 权限等级配置（set_level/current_level 读写 model.json）
import model_config as mc
mc.CONFIG_PATH = tmp / "model.json"
import ai_levels
ai_levels.set_level("operate")
import ai_mode
original_ai_mode_path = ai_mode.CONFIG_PATH
ai_mode.CONFIG_PATH = tmp / "ai_mode.json"
ai_mode.set_mode("auto")
from healers.command_runner import healer as command_healer
repair_patcher = mock.patch.object(
    command_healer, "heal",
    return_value={"success": True, "actions": [{"check_ok": True}]})
repair_patcher.start()

fp = "process:自动技能测试"
fake = {
    "fingerprint": {"type": "port_dead", "service": "3971"},
    "root_cause": "服务未自启",
    "fix_direction": "先检查端口，再重启服务",
    "fix_command": "systemctl restart x",
    "confidence": 0.85,
}

# 场景: 规则修复失败 → AI 诊断 → 自动修复回检成功 → 生成 skill
with mock.patch.object(ai_diagnose, "_call_llm", return_value=fake):
    ai_diagnose._diagnose_worker(fp, "自动技能测试", "process", {}, None, None)

skills = promotion.load_skills()
assert len(skills) == 1, skills
s = skills[0]
assert s["status"] == "pending_confirm" and s["source"] == "ai_auto"
print("1. AI自动修复并回检成功 -> 自动生成待审批技能 OK")
print("   候选命令:", s["command"])
assert s["command"] == "systemctl restart x"
assert s["port"] == 3971

rules = config.load_rules().get("rules") or []
assert not any(r.get("healer") == "command_runner" for r in rules)
print("2. 待审批技能不影响 rules.yaml OK")

with mock.patch.object(ai_diagnose, "_call_llm", return_value=fake):
    ai_diagnose._diagnose_worker(fp, "自动技能测试", "process", {}, None, None)
assert len(promotion.load_skills()) == 1
print("3. 同指纹不重复生成 OK")

confirmed = promotion.confirm_skill(s["id"])
s_after = promotion.get_skill(s["id"])
active = [r for r in config.load_rules()["rules"]
          if r.get("_skill_id") == s["id"]]
assert s_after["status"] == "active" and active and active[0]["enabled"]
print("4. 管理员 confirm 后生效 OK")

# 场景: LLM 单独输出 fix_command 时，技能命令直接采用它
# （而不是从 fix_direction 里提取到诊断类命令）
ai_levels.set_level("operate")   # restart 类命令需 operate 级才允许生成
fp2 = "process:修复命令直取"
fake2 = {
    "fingerprint": {"type": "port_dead", "service": "3972"},
    "root_cause": "服务崩溃",
    "fix_direction": "建议先执行 `journalctl -u myapp -n 50` 查看日志",
    "fix_command": "systemctl restart myapp",
    "confidence": 0.9,
}
with mock.patch.object(ai_diagnose, "_call_llm", return_value=fake2):
    ai_diagnose._diagnose_worker(fp2, "修复命令直取", "process", {}, None, None)
s2 = [x for x in promotion.load_skills() if x["fingerprint"] == fp2][0]
assert s2["command"] == "systemctl restart myapp", s2["command"]
print("5. fix_command 直取（不误提取诊断命令）OK:", s2["command"])
promotion.confirm_skill(s2["id"])   # 先生效，供下面验证并入时同步 rules.yaml

# 场景: 同指纹复发 → 查重命中已有技能 → 不新建，生成 duplicate 并入提案
skills_before = len(promotion.load_skills())
fake3 = dict(fake2, fix_command="systemctl restart myapp2")
with mock.patch.object(ai_diagnose, "_call_llm", return_value=fake3):
    ai_diagnose._diagnose_worker(fp2, "修复命令直取", "process", {}, None, None)
assert len(promotion.load_skills()) == skills_before, "查重后不应新建技能"
dups = [d for d in promotion.load() if d.get("status") == "duplicate"
        and d.get("existing_skill_id") == s2["id"]]
assert dups and dups[0]["command_changed"], dups
print("6. 技能查重：类似故障生成并入提案而非重复技能 OK")

# 场景: 人工审核并入 → active 技能命令更新且 rules.yaml 规则同步
merged = promotion.merge_duplicate(dups[0]["id"])
assert merged["command"] == "systemctl restart myapp2"
assert merged["merge_count"] == 1
rule = [r for r in config.load_rules()["rules"]
        if r.get("_skill_id") == s2["id"]][0]
assert rule["params"]["command"] == "systemctl restart myapp2"
print("7. 并入审核：active 技能与 rules.yaml 同步更新 OK")

repair_patcher.stop()
ai_mode.CONFIG_PATH = original_ai_mode_path
