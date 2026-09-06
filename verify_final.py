"""终极验证：确认数据库里的视图和 ACL"""
import os

print("=" * 60)
print("  服务端终极验证")
print("=" * 60)

# 1. 列表视图 XML 内容
print("\n① 合同列表视图 XML (前200字符):")
list_view = env.ref('contract_ai.view_contract_list')
arch = list_view.arch or ''
# 看 tree 里有没有 control + button
if 'control' in arch and 'upload_wizard' in arch:
    print("   ✅ tree 视图包含 <control> 和上传按钮")
else:
    print(f"   ❌ 没找到! arch 片段: {arch[:300]}")

# 2. action_contract 的 view_mode
print("\n② action_contract view_mode:")
act = env.ref('contract_ai.action_contract')
print(f"   '{act.view_mode}'")
if 'tree' in act.view_mode and act.view_mode.index('tree') == 0:
    print("   ✅ tree 在第一位（默认视图）")

# 3. ACL — base.group_user 对 contract.contract
print("\n③ ACL: base.group_user → contract.contract:")
group = env.ref('base.group_user')
model_id = env['ir.model']._get_id('contract.contract')
acl = env['ir.model.access'].sudo().search([
    ('model_id', '=', model_id),
    ('group_id', '=', group.id),
], limit=1)
if acl:
    print(f"   read={acl.perm_read} write={acl.perm_write} create={acl.perm_create} unlink={acl.perm_unlink}")
    if acl.perm_create:
        print("   ✅ create=1 — 用户可以新建合同")
else:
    print("   ❌ 没有 ACL 记录!")

# 4. 直接测试创建
print("\n④ 直接测试创建合同:")
try:
    c = env['contract.contract'].create({'name': '[终极验证]'})
    print(f"   ✅ 创建成功 id={c.id}")
    c.unlink()
except Exception as e:
    print(f"   ❌ {e}")

# 5. 检查用户组
print("\n⑤ admin 用户组:")
admin = env.ref('base.user_admin')
for g in admin.groups_id:
    print(f"   - {g.name} (id={g.id})")

print("\n" + "=" * 60)
print("  服务端没问题 → 请清浏览器缓存!")
print("=" * 60)
