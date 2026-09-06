# 单文件复测：租赁合同付款计划展开验证
import base64
import sys
import time
import xmlrpc.client

DB, USER, PWD = "contract_db", "admin", "admin"
common = xmlrpc.client.ServerProxy("http://localhost:8069/xmlrpc/2/common")
uid = common.authenticate(DB, USER, PWD, {})
models = xmlrpc.client.ServerProxy("http://localhost:8069/xmlrpc/2/object")


def call(model, method, params, **kw):
    return models.execute_kw(DB, uid, PWD, model, method, params, kw)


with open(r"d:\Project\langchain\contract-system\test_pdfs\manual\扫描版_房屋租赁合同_ZL2025-007.pdf", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

wid = call("contract.upload.wizard", "create", [{"pdf_file": b64, "pdf_filename": "租赁复测.pdf"}])
acts = call("contract.upload.wizard", "action_create_and_extract", [[wid]])
cid = acts[0]["res_id"]
print("contract_id:", cid)
rec = None
for i in range(60):
    time.sleep(3)
    rec = call("contract.contract", "read", [cid],
               fields=["ai_extract_status", "amount", "name"])[0]
    if rec["ai_extract_status"] in ("success", "failed"):
        break
print("status:", rec["ai_extract_status"], "| amount:", rec["amount"])
plans = call("contract.payment.plan", "search_read",
             [[("contract_id", "=", cid)]], fields=["name", "planned_amount", "milestone"])
total = sum(p["planned_amount"] or 0 for p in plans)
print("plans:", len(plans), "| total:", total)
for p in plans:
    print("  -", p["name"], p["planned_amount"], p["milestone"])
