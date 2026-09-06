# ═══════════════════════════════════════════════════════════════════
# e2e 验证三项修复：
#   1. 向导上传扫描版 PDF → 后台 AI 识别耗时
#   2. 识别期间表单不阻塞（name 允许为空）
#   3. 条款/元素自动生成落库
# ═══════════════════════════════════════════════════════════════════
import base64
import sys
import time
import xmlrpc.client

URL = "http://localhost:8069"
DB, USER, PWD = "contract_db", "admin", "admin"
PDF_PATH = r"D:\Project\langchain\contract-system\test_pdfs\manual\扫描版_房屋租赁合同_ZL2025-007.pdf"

common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
uid = common.authenticate(DB, USER, PWD, {})
models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")
print(f"authenticated uid={uid}")
assert uid, "login failed"


def call(model, method, *args, **kw):
    return models.execute_kw(DB, uid, PWD, model, method, list(args), kw)


pdf_b64 = base64.b64encode(open(PDF_PATH, "rb").read()).decode()

# ── 1. 模拟向导：创建 wizard → action_create_and_extract ──
wiz_id = call("contract.upload.wizard", "create", {
    "pdf_file": pdf_b64,
    "pdf_filename": "扫描版_房屋租赁合同_ZL2025-007.pdf",
})
t0 = time.time()
result = call("contract.upload.wizard", "action_create_and_extract", [wiz_id])
# 修复后向导返回单个 act_window dict（Odoo 17 前端不接受 action 数组）
contract_id = result["res_id"] if isinstance(result, dict) else result[0]["res_id"]
print(f"[1] 向导创建合同 id={contract_id}，已提交后台识别（返回耗时 {time.time()-t0:.1f}s）")

# ── 2. 识别期间立刻保存空名称记录（问题2验证：不再阻塞）──
#     write 一个无关字段模拟用户在识别中点保存
call("contract.contract", "write", [contract_id], {"payment_terms": "（识别期间用户编辑测试）"})
print("[2] 识别期间写库成功 → 空名称不再阻塞保存")

# ── 3. 轮询识别状态 ──
timeout = 240
while time.time() - t0 < timeout:
    rec = call("contract.contract", "read", [contract_id],
               fields=["ai_extract_status", "name", "amount", "date_signed", "ai_extract_error"])[0]
    if rec["ai_extract_status"] != "running":
        break
    time.sleep(5)
elapsed = time.time() - t0
print(f"[3] AI 识别结束 | status={rec['ai_extract_status']} | 耗时 {elapsed:.0f}s")
print(f"    name={rec['name']} | amount={rec['amount']} | date_signed={rec['date_signed']}")
if rec["ai_extract_status"] != "success":
    print(f"    error={rec['ai_extract_error']}")
    sys.exit(1)

# ── 4. 条款/元素落库验证 ──
clauses = call("contract.contract", "read", [contract_id], fields=["clause_ids"])[0]["clause_ids"]
elements = call("contract.contract", "read", [contract_id], fields=["element_ids"])[0]["element_ids"]
print(f"[4] 合同条款 {len(clauses)} 条 | 合同元素 {len(elements)} 个")
if clauses:
    for c in call("contract.clause", "read", clauses,
                  fields=["sort_order", "name", "clause_type", "source"]):
        print(f"    条款[{c['sort_order']}] {c['name']} | {c['clause_type']} | {c['source']}")
if elements:
    for e in call("contract.element", "read", elements,
                  fields=["name", "element_key", "value_text", "source"]):
        print(f"    元素 {e['name']}({e['element_key']}) = {str(e['value_text'])[:30]} | {e['source']}")

ok = len(clauses) > 0 and len(elements) > 0 and rec["name"]
print("\n✅ E2E 全部通过" if ok else "\n❌ E2E 存在缺失", f"| contract_id={contract_id}")
sys.exit(0 if ok else 2)
