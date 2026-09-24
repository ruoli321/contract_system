"""把 admin 用户加入合同管理员组 + 给 base.group_user 补全所有模型 ACL"""

# 方法 1: 把 admin 用户加入 contract manager 组
admin = env.ref('base.user_admin')
manager_group = env.ref('contract_ai.group_contract_manager')
if manager_group not in admin.groups_id:
    admin.write({'groups_id': [(4, manager_group.id)]})
    print(f"✅ admin 已加入组: {manager_group.name} (id={manager_group.id})")
else:
    print(f"✅ admin 已在组里: {manager_group.name}")

# 方法 2: 给 base.group_user 补全所有模型 ACL（双保险）
base_user = env.ref('base.group_user')

acl_map = [
    # (ACL id, 模型 xmlid, name)
    ('access_counterparty_all', 'contract_ai.model_contract_counterparty', 'contract.counterparty'),
    ('access_signatory_all', 'contract_ai.model_contract_signatory', 'contract.signatory'),
    ('access_template_all', 'contract_ai.model_contract_template', 'contract.template'),
    ('access_clause_all', 'contract_ai.model_contract_clause', 'contract.clause'),
    ('access_element_all', 'contract_ai.model_contract_element', 'contract.element'),
    ('access_payment_plan_all', 'contract_ai.model_contract_payment_plan', 'contract.payment.plan'),
    ('access_config_all', 'contract_ai.model_contract_config', 'contract.config'),
]

for acl_id, model_xmlid, model_name in acl_map:
    existing = env['ir.model.access'].search([
        ('model_id', '=', env.ref(model_xmlid).id),
        ('group_id', '=', base_user.id),
    ], limit=1)
    if not existing:
        env['ir.model.access'].create({
            'name': f"{model_name}.all",
            'model_id': env.ref(model_xmlid).id,
            'group_id': base_user.id,
            'perm_read': True,
            'perm_write': True,
            'perm_create': True,
            'perm_unlink': True,
        })
        print(f"  ✅ 补 ACL: base.group_user → {model_name}")
    else:
        # 升级权限为全量
        existing.write({
            'perm_read': True, 'perm_write': True,
            'perm_create': True, 'perm_unlink': True,
        })
        print(f"  ✅ ACL 已存在: base.group_user → {model_name} (已确保全开)")

print("\n🎉 修复完成！刷新浏览器即可。")
