import re, sys

# 在 Odoo shell 中运行的脚本
self.env.cr.execute("UPDATE ir_ui_view SET active='t' WHERE model='contract.contract'")
self.env.cr.execute("DELETE FROM ir_attachment WHERE name LIKE '%contract%'")
self.env.clear_caches()
print("✅ 缓存已清除", flush=True)

self.env.cr.execute("SELECT arch_db::text FROM ir_ui_view WHERE model='contract.contract' AND type='form'")
arch = self.env.cr.fetchone()[0]
m = re.search(r'业务条款.{0,400}', arch, re.DOTALL)
print("\n📋 DB 中的业务条款 tab:", flush=True)
print(m.group(0) if m else "NOT FOUND", flush=True)

sys.exit(0)
