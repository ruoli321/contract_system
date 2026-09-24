"""快速检查：所有旧字段名/旧方法名/旧 state 值是否已从视图层清除"""
import os, re

BASE = r"d:\Project\langchain\contract-system\odoo\addons\contract_ai"

# M3 重构前的旧标识 → 新标识
OLD_FIELDS = [
    "contract_type",  # → type
    "sign_date",      # → date_signed
    "effective_date", # → date_start
    "expire_date",    # → date_end
    "partner_a_id",   # → partner_a (M2O)
    "partner_b_id",   # → partner_b (M2O)
    "pdf_file",       # → source_pdf
    "pdf_filename",   # → source_pdf_filename
]

OLD_METHODS = [
    "action_cancel",      # → action_void
    "action_approving",   # 不存在了
]

OLD_STATES = [
    "approving", "approved", "sealed", "cancelled",  # 合同主表 state
]

# 扫描 views/ 目录下所有 XML
xml_dir = os.path.join(BASE, "views")
all_text = ""
for fn in sorted(os.listdir(xml_dir)):
    if not fn.endswith(".xml"):
        continue
    fp = os.path.join(xml_dir, fn)
    content = open(fp, encoding="utf-8").read()
    all_text += f"\n=== {fn} ===\n" + content

print("=" * 60)
print("旧字段/方法/State 检查（仅扫描 views/*.xml）")
print("=" * 60)

found = False
# 1. 旧字段
for old in OLD_FIELDS:
    matches = re.findall(rf'\b{old}\b', all_text)
    if matches:
        print(f"⚠️ 旧字段 '{old}' 出现 {len(matches)} 次")
        found = True

# 2. 旧方法
for old in OLD_METHODS:
    matches = re.findall(rf'name="{old}"', all_text)
    if matches:
        print(f"⚠️ 旧方法 '{old}' 出现 {len(matches)} 次")
        found = True

# 3. 旧 state 值
for old in OLD_STATES:
    matches = re.findall(rf"['\"]{old}['\"]", all_text)
    if matches:
        print(f"⚠️ 旧 state 值 '{old}' 出现 {len(matches)} 次")
        found = True

if not found:
    print("✅ 视图层无任何 M3 重构前的旧引用！")

# 4. 新字段全部存在性检查
print("\n" + "=" * 60)
print("contract_views.xml 新字段存在性（抽查）")
print("=" * 60)
cv = open(os.path.join(xml_dir, "contract_views.xml"), encoding="utf-8").read()
checks = {
    "type": "合同类型字段",
    "date_signed": "签订日期",
    "date_start": "生效日期",
    "date_end": "失效日期",
    "partner_a": "甲方 Many2one",
    "partner_b": "乙方 Many2one",
    "source_pdf": "源文件",
    "source_pdf_filename": "文件名",
    "signatory_id": "签约人",
    "dispute_resolution": "争议解决",
    "action_ai_extract": "AI 按钮",
    "action_approve": "提交审批",
    "action_void": "作废",
    "draft": "草拟",
    "approval": "审批中",
    "seal": "已用印",
    "archived": "已归档",
    "void": "已作废",
}
for fname, desc in checks.items():
    if f'name="{fname}"' in cv or f"'{fname}'" in cv or fname in cv.lower():
        print(f"  ✅ {fname} ({desc})")
    else:
        print(f"  ❌ {fname} ({desc}) 未找到！")

# 5. 验证 action_contract 菜单绑定
print("\n" + "=" * 60)
print("菜单 / 动作绑定检查")
print("=" * 60)
menu = open(os.path.join(xml_dir, "contract_menu.xml"), encoding="utf-8").read()
for act in ["action_contract", "action_contract_counterparty",
            "action_contract_signatory", "action_contract_template"]:
    if act in menu:
        print(f"  ✅ {act} 已绑定菜单")
    else:
        print(f"  ❌ {act} 未绑定菜单！")

print("\nDone.")
