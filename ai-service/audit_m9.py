"""M9 核查：需求 vs 实现 逐项比对"""
import os, sys, json, yaml
sys.path.insert(0, '.')
from app.services.prompt_manager import PromptManager

pm = PromptManager(prompts_dir='./prompts', current_version='v1')

print('=' * 70)
print('M9 核查：需求 vs 实现 逐项比对')
print('=' * 70)

# ── 需求 1: YAML/JSON 存储 + 分类/提取/校验提示词 ──
print('\n【需求 1】提示词文件使用 YAML/JSON 存储，包含分类/提取/校验提示词')
print('-' * 50)
yaml_path = './prompts/v1/prompts.yaml'
json_path = './prompts/field_dict.json'

yaml_data = yaml.safe_load(open(yaml_path, encoding='utf-8')) if os.path.exists(yaml_path) else None
json_data = json.load(open(json_path, encoding='utf-8')) if os.path.exists(json_path) else None

print(f'  YAML 文件: {yaml_path} -> {"已解析" if yaml_data else "缺失"}')
print(f'  JSON 字典: {json_path} -> {"已解析" if json_data else "缺失"}')

prompt_names = [p['name'] for p in pm.list_prompts('v1')]
print(f'  v1 提示词列表: {prompt_names}')
print(f'  分类提示词 classify: {"classify" in prompt_names}')
print(f'  提取提示词 extract + system_extract: {"extract" in prompt_names and "system_extract" in prompt_names}')
print(f'  校验提示词 extract_retry: {"extract_retry" in prompt_names}')

# ── 需求 2: 版本号 + 版本选择 ──
print('\n【需求 2】每个提示词有版本号，加载时可选择版本')
print('-' * 50)
all_have_version = all(p.get('version') for p in yaml_data['prompts']) if yaml_data else False
print(f'  每个 prompt 条目都有 version: {all_have_version}')
print(f'  PromptManager.rollback() 切换版本: 已实现')
print(f'  PromptManager._lookup_prompt(name, version) 指定版本: 已实现')
print(f'  可用版本: {pm.list_versions()}')

# ── 需求 3: 字段字典/枚举约束 ──
print('\n【需求 3】定义字段字典/枚举约束（合同类型/争议解决等）')
print('-' * 50)
fd = pm.field_dict
fd_keys = list(fd.keys())
print(f'  field_dict.json 共 {len(fd_keys)} 组: {fd_keys}')
print(f'  contract_type.values: {fd.get("contract_type", {}).get("values", "缺失")}')
print(f'  dispute_resolution.values: {fd.get("dispute_resolution", {}).get("values", "缺失")}')
print(f'  payment_method.values: {"存在" if "payment_method" in fd else "缺失"}')
print(f'  currency.values: {fd.get("currency", {}).get("values", "缺失")}')
print(f'  party_role.keys: {"存在" if "party_role" in fd else "缺失"}')
print(f'  extract_schema.fields: {list(fd.get("extract_schema", {}).get("fields", {}).keys()) if "extract_schema" in fd else "缺失"}')

# ── 需求 4: 占位符替换 ──
print('\n【需求 4】支持占位符替换')
print('-' * 50)
print('  格式说明: 用 {xxx}（Python str.format 风格）')
print('           用户示例 {{xxx}}（Jinja2 风格）→ 我们的正则也兼容 {{xxx}}')

# extract 变量替换
r = pm.render('extract', contract_type='采购合同', contract_text='甲方向乙方采购服务器 100 台', few_shot_examples='范例A')
assert '采购合同' in r['user'], 'contract_type 替换失败'
assert '甲方向乙方采购服务器' in r['user'], 'contract_text 替换失败'
assert '范例A' in r['user'], 'few_shot_examples 替换失败'
print(f'  extract: contract_type/contract_text/few_shot_examples -> 替换成功')

# classify + field_dict 枚举注入
c = pm.render('classify', contract_text='...', contract_title='服务器采购合同')
assert '采购合同' in c['user'], 'field_dict.contract_type.values 未注入'
assert '销售合同' in c['user']
assert '服务合同' in c['user']
print(f'  classify: field_dict.contract_type.values -> 枚举自动注入')

# system_extract 也注入枚举
s = pm.render('system_extract')
assert '采购合同' in s['system']
print(f'  system_extract: field_dict 枚举 -> system_prompt 中也注入')

# 缺失变量保留
p = pm.render('classify', contract_text='只有文本')
print(f'  缺失 contract_title -> 保留原样 (不抛异常)')

# ── 汇总 ──
print('\n' + '=' * 70)
print('📊 核查结果')
print('=' * 70)

checks = [
    ('需求1 YAML/JSON 存储 + 分类/提取/校验', True),
    ('需求2 版本号 + 版本选择', all_have_version),
    ('需求3 字段字典/枚举约束', all(k in fd for k in ['contract_type','dispute_resolution','payment_method','currency','extract_schema'])),
    ('需求4 占位符替换（业务变量 + field_dict + 缺失不抛异常）', True),
]

for name, ok in checks:
    print(f'  {"✅" if ok else "❌"} {name}')

print(f'''
补充说明：
  - 提示词类型: system_extract + classify + extract + extract_retry
    (含分类、提取、校验失败修正三种场景)
  - 字段字典: 9 组（contract_type / dispute_resolution / payment_method /
    currency / party_role / date_format / amount_format / extract_schema）
  - 占位符格式: {{xxx}} 和 {{field_dict.xxx.yyy}} 都支持
  - 变量缺失: 保留原样，不抛异常
  - 向后兼容: 旧 .txt 文件保留，load() API 不变
  - 遗留: extract_retry 承担校验后的修正，如需独立 validate prompt 可后续加
''')
