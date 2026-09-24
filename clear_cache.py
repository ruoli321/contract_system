"""清 Odoo 缓存"""
import os

print("清 Odoo 缓存...")

# 清 ir.attachment 里的 asset bundle
env.cr.execute("""
    DELETE FROM ir_attachment
    WHERE res_model = 'ir.ui.view'
    AND name LIKE 'web.assets_%'
""")
print(f"  ✅ 清除 asset bundles: {env.cr.rowcount} 条")

# 清 ir.ui.view 的 cache
env.cr.execute("UPDATE ir_ui_view SET arch_db = NULL WHERE arch_db IS NOT NULL")
print(f"  ✅ 清除 view arch_db 缓存: {env.cr.rowcount} 条")

# 清 caches
try:
    from odoo.tools.cache import clear_caches
    clear_caches()
    print("  ✅ 运行时缓存已清")
except:
    pass

print("\n缓存已清！请在浏览器 Ctrl+Shift+R 强制刷新。")
