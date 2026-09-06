"""验证侧边栏菜单"""
print("=== 侧边栏菜单 ===")
root = env.ref('contract_ai.menu_contract_root')
for child in root.child_id.sorted(key=lambda m: m.sequence):
    act_name = child.action.name if child.action else '(无动作)'
    print(f"  [{child.sequence:3d}] {child.name:30s} → {act_name}")
print("\n✅ 最顶部应该看到: 📄 上传 PDF · AI 识别")
