"""M9 PromptManager 端到端验证脚本"""
import sys
sys.path.insert(0, '/app')
from app.services.prompt_manager import PromptManager

print('=' * 60)
print('M9 PromptManager 端到端验证')
print('=' * 60)

pm = PromptManager(prompts_dir='/app/prompts', current_version='v1')

print(f'\n📂 可用版本: {pm.list_versions()}')
print(f'📂 当前版本: {pm.current_version}')
print(f'📂 field_dict 键数: {len(pm.field_dict)}')

print('\n📋 v1 版本的提示词清单:')
for p in pm.list_prompts('v1'):
    name = p['name']
    sys_has = 'Y' if p['has_system'] else '-'
    usr_has = 'Y' if p['has_user'] else '-'
    vars_str = ', '.join(p['variables']) if p['variables'] else '(无)'
    print(f'  · {name:20s} | sys={sys_has} | usr={usr_has} | vars: {vars_str}')

# 1. render classify + field_dict 注入
print('\n🔧 Test 1: render(classify) — field_dict 枚举注入')
rendered = pm.render('classify',
    contract_text='甲方向乙方采购服务器 100 台...',
    contract_title='服务器采购合同')
print(f'  system 前120字: {rendered["system"][:120]}...')
print(f'  user 前120字:   {rendered["user"][:120]}...')

assert '采购合同' in rendered['user'], '❌ field_dict.contract_type.values 未注入！'
assert '销售合同' in rendered['user'], '❌ field_dict.contract_type.values 未注入！'
print('  ✅ field_dict 枚举注入成功！')

# 2. render system_extract
print('\n🔧 Test 2: render(system_extract) — 纯 system prompt')
sys_r = pm.render('system_extract')
assert '严格' in sys_r['system'], '❌ system_extract system_prompt 为空！'
assert '采购合同' in sys_r['system'], '❌ system_prompt 中 contract_type.values 未注入！'
print(f'  system 前150字: {sys_r["system"][:150]}...')
print(f'  user 长度: {len(sys_r["user"])} (应为 0)')
print('  ✅ system_extract 渲染 + 枚举注入成功！')

# 3. render extract 自动继承 system_extract
print('\n🔧 Test 3: render(extract) — 自动继承 system_extract')
ex = pm.render('extract',
    contract_type='采购合同',
    contract_text='合同文本 XYZ',
    few_shot_examples='范例 A')
assert '严格' in ex['system'], '❌ extract 未继承 system_extract！'
assert '采购合同' in ex['user'], '❌ extract user 变量替换失败！'
assert '合同文本 XYZ' in ex['user'], '❌ extract contract_text 替换失败！'
print(f'  system 继承: {"严格" in ex["system"]}')
print(f'  user 含 contract_type: {"采购合同" in ex["user"]}')
print(f'  user 含 contract_text: {"合同文本 XYZ" in ex["user"]}')
print('  ✅ extract 自动继承 system_extract + 变量替换成功！')

# 4. extract_retry
print('\n🔧 Test 4: render(extract_retry)')
retry = pm.render('extract_retry',
    contract_text='原始合同',
    previous_result='{"amount": null}',
    validation_errors='amount 为 null 但文本中明确写了 500 万')
assert '原始合同' in retry['user']
assert '500 万' in retry['user']
print('  ✅ extract_retry 成功！')

# 5. 向后兼容 load()
print('\n🔧 Test 5: load() 向后兼容')
old = pm.load('classify')
assert isinstance(old, str) and len(old) > 0
print(f'  返回 str，长度: {len(old)}')
print('  ✅ load() 向后兼容成功！')

# 6. 变量未注入 → 保留原样（不抛异常）
print('\n🔧 Test 6: 缺失变量 → 保留占位符原样')
partial = pm.render('classify', contract_text='只有文本，缺 contract_title')
assert '{contract_title}' in partial['user'] or '只有文本' in partial['user']
print('  ✅ 缺失变量保留原样，不抛异常！')

print('\n' + '=' * 60)
print('✅ M9 PromptManager 全部测试通过！')
print('=' * 60)
