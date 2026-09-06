# 端到端测试：XML-RPC 真实上传三个 PDF（2 扫描版 + 1 文字版）
# 流程：向导上传 → 后台异步解析/OCR/LLM 提取 → 轮询状态 → 校验字段入库
import base64
import os
import sys
import time
import xmlrpc.client

URL = "http://localhost:8069"
DB = "contract_db"
USER = "admin"
PWD = "admin"
POLL_INTERVAL = 3
POLL_TIMEOUT = 240  # 每个合同最长等待

MANUAL_DIR = r"d:\Project\langchain\contract-system\test_pdfs\manual"
FILES = [
    "文字版_办公设备采购合同_CG2025-018.pdf",
    "扫描版_软件开发服务合同_RW2025-021.pdf",
    "扫描版_房屋租赁合同_ZL2025-007.pdf",
]

common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
uid = common.authenticate(DB, USER, PWD, {})
if not uid:
    print("LOGIN FAILED")
    sys.exit(1)
models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")
print(f"login ok uid={uid}")


def call(model, method, params=None, **kw):
    return models.execute_kw(DB, uid, PWD, model, method, params or [], kw)


def poll_contract(cid):
    """轮询 ai_extract_status 直到 done/error，返回最终记录"""
    t0 = time.time()
    last = None
    while time.time() - t0 < POLL_TIMEOUT:
        recs = call("contract.contract", "read", [cid],
                    fields=["ai_extract_status", "ai_extract_error", "name", "code",
                            "state", "type", "amount", "amount_uppercase",
                            "date_signed", "date_start", "date_end",
                            "extraction_confidence"])
        r = recs[0]
        st = r["ai_extract_status"]
        if st != last:
            print(f"  [{time.time()-t0:5.1f}s] status={st}")
            last = st
        if st in ("success", "failed"):
            return r, time.time() - t0
        time.sleep(POLL_INTERVAL)
    return None, time.time() - t0


results = []
for fname in FILES:
    path = os.path.join(MANUAL_DIR, fname)
    print(f"\n{'='*70}\n▶ 上传: {fname}")
    with open(path, "rb") as f:
        pdf_b64 = base64.b64encode(f.read()).decode()

    wizard_id = call("contract.upload.wizard", "create",
                     [{"pdf_file": pdf_b64, "pdf_filename": fname}])
    t0 = time.time()
    actions = call("contract.upload.wizard", "action_create_and_extract", [[wizard_id]])
    ret = round(time.time() - t0, 1)
    # 从返回 action 中取 res_id
    cid = None
    if isinstance(actions, list):
        for a in actions:
            if isinstance(a, dict) and a.get("res_id"):
                cid = a["res_id"]
                break
    print(f"  action 返回耗时 {ret}s (应远小于60s) -> contract_id={cid}")
    if not cid:
        print("  ❌ 未取到 contract_id，跳过")
        results.append((fname, None, ret, "no contract id"))
        continue

    rec, waited = poll_contract(cid)
    if rec is None:
        print(f"  ❌ 超时 {waited:.0f}s 未完成")
        results.append((fname, cid, waited, "TIMEOUT"))
        continue

    status = rec["ai_extract_status"]
    print(f"  状态={status} 用时 {waited:.0f}s")
    if status == "error":
        print(f"  ❌ 错误: {rec['ai_extract_error']}")
        results.append((fname, cid, waited, f"ERROR: {rec['ai_extract_error']}"))
        continue

    # 关联数据
    plans = call("contract.payment.plan", "search_count", [[("contract_id", "=", cid)]])
    elements = call("contract.element", "search_count", [[("contract_id", "=", cid)]])
    clauses = call("contract.clause", "search_count", [[("contract_id", "=", cid)]])
    partners = call("contract.contract", "read", [cid], fields=["partner_a", "partner_b"])
    pa = partners[0]["partner_a"]
    pb = partners[0]["partner_b"]

    print(f"  name={rec['name']}")
    print(f"  code={rec['code']} state={rec['state']} type={rec['type']} conf={rec['extraction_confidence']}")
    print(f"  amount={rec['amount']} (大写:{rec['amount_uppercase']})")
    print(f"  签订={rec['date_signed']} 起始={rec['date_start']} 到期={rec['date_end']}")
    print(f"  甲方={pa[1] if pa else None} 乙方={pb[1] if pb else None}")
    print(f"  入库: 付款计划={plans} elements={elements} clauses={clauses}")
    results.append((fname, cid, waited, "OK"))

print(f"\n{'='*70}\n■ 汇总")
all_ok = True
for fname, cid, took, note in results:
    mark = "✅" if note == "OK" else "❌"
    if note != "OK":
        all_ok = False
    print(f" {mark} {fname}: id={cid} {took:.0f}s {note}")
sys.exit(0 if all_ok else 2)
