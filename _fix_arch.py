# 临时脚本：修复 contract.contract form 视图里残留的 template_id
import re
import json

# Odoo shell 环境中执行
views = env['ir.ui.view'].sudo().search([
    ('model', '=', 'contract.contract'),
    ('arch_db', 'like', '%template_id%'),
])

for view in views:
    print(f"Processing view {view.id}: {view.name}")
    arch = view.arch_db
    print(f"  Before: contains template_id = {'template_id' in arch if isinstance(arch, str) else any('template_id' in str(v) for v in arch.values()) if isinstance(arch, dict) else False}")
    
    # arch_db 可能是 dict (jsonb) 或 str
    if isinstance(arch, dict):
        for lang, xml_text in arch.items():
            if 'template_id' in str(xml_text):
                # 删除 template_id 那一行（不管前后空格）
                cleaned = re.sub(
                    r'\n\s*<field name="template_id"/>\s*', 
                    '\n', 
                    str(xml_text)
                )
                if cleaned == str(xml_text):
                    # 如果多行匹配失败，尝试只删元素不管换行
                    cleaned = re.sub(
                        r'<field name="template_id"/>', 
                        '', 
                        str(xml_text)
                    )
                arch[lang] = cleaned
                print(f"  Cleaned {lang}: {'template_id' in cleaned}")
        view.arch_db = arch
    elif isinstance(arch, str):
        cleaned = re.sub(r'\n\s*<field name="template_id"/>\s*', '\n', arch)
        cleaned = re.sub(r'<field name="template_id"/>', '', cleaned)
        view.arch_db = cleaned
    
    view.invalidate_recordset()
    print(f"  Fixed!")

print("Done.")
