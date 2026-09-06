"""验证 M18 修复：ACL + Wizard + view_mode"""
# 直接在 Odoo shell 里 import
import sys

print("=" * 60)
print("  M18 修复验证")
print("=" * 60)

# 1. 检查 wizard 模型
print("\n① Wizard 模型 (contract.upload.wizard):")
try:
    WizardModel = env['ir.model'].sudo()
    found = WizardModel.search([('model', '=', 'contract.upload.wizard')])
    if found:
        print(f"   ✅ 模型已注册: {found.name} (id={found.id})")
    else:
        print("   ❌ 模型未注册!")
except Exception as e:
    print(f"   ❌ 异常: {e}")

# 2. ACL 检查
print("\n② ACL 权限 (admin 用户创建合同):")
admin = env.ref('base.user_admin')
try:
    admin._check_writable('contract.contract')
    print("   ✅ admin 用户有 write 权限")
except Exception as e:
    print(f"   ❌ admin 无权限: {e}")

# 3. 直接尝试创建合同
try:
    c = env['contract.contract'].sudo().create({'name': '[验证测试] 临时合同'})
    print(f"   ✅ 可以创建合同 (id={c.id})")
    c.sudo().unlink()
    print("   ✅ 临时合同已清理")
except Exception as e:
    print(f"   ❌ 创建合同失败: {e}")

# 4. view_mode 检查
print("\n③ action_contract 视图顺序:")
try:
    act = env.ref('contract_ai.action_contract')
    print(f"   view_mode = '{act.view_mode}'")
    if act.view_mode.startswith('tree'):
        print("   ✅ tree 是默认视图")
    else:
        print("   ⚠️ tree 不是默认视图")
except Exception as e:
    print(f"   ❌ 异常: {e}")

# 5. Wizard action
print("\n④ Wizard action:")
try:
    wiz_act = env.ref('contract_ai.action_contract_upload_wizard')
    print(f"   ✅ 存在: {wiz_act.name} (target={wiz_act.target})")
except Exception as e:
    print(f"   ❌ 不存在: {e}")

# 6. 列表页 XML 按钮
print("\n⑤ 列表页上传按钮:")
try:
    list_view = env.ref('contract_ai.view_contract_list')
    arch = list_view.arch or ''
    if 'upload_wizard' in arch or 'fa-upload' in arch:
        print("   ✅ XML 包含上传按钮")
    else:
        print("   ❌ XML 找不到上传按钮")
except Exception as e:
    print(f"   ❌ 异常: {e}")

print("\n" + "=" * 60)
print("  验证完成")
print("=" * 60)
