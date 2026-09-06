# ═══════════════════════════════════════════════════════════════════
# 回归验证：合同 117（扫描版租赁合同）重新识别 → 付款计划应生成 4 期
# ═══════════════════════════════════════════════════════════════════
import sys
import time
import xmlrpc.client

URL = "http://localhost:8069"
DB, USER, PWD = "contract_db", "admin", "admin"

common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
uid = common.authenticate(DB, USER, PWD, {})
models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")


def call(model, method, *args, **kw):
    return models.execute_kw(DB, uid, PWD, model, method, list(args), kw)


cid = int(sys.argv[1]) if len(sys.argv) > 1 else 117
t0 = time.time()
call("contract.contract", "action_ai_extract", [cid])
print(f"[1] 合同 {cid} 已重新提交识别")

while time.time() - t0 < 240:
    rec = call("contract.contract", "read", [cid],
               fields=["ai_extract_status", "ai_extract_error"])[0]
    if rec["ai_extract_status"] != "running":
        break
    time.sleep(5)
print(f"[2] 识别结束 | status={rec['ai_extract_status']} | 耗时 {time.time()-t0:.0f}s")
if rec["ai_extract_status"] != "success":
    print(f"    error={rec['ai_extract_error']}")
    sys.exit(1)

plan_ids = call("contract.contract", "read", [cid], fields=["payment_plan_ids"])[0]["payment_plan_ids"]
plans = call("contract.payment.plan", "read", plan_ids,
             fields=["sort_order", "name", "planned_amount", "planned_date"]) if plan_ids else []
print(f"[3] 付款计划 {len(plans)} 期")
total = 0.0
for p in sorted(plans, key=lambda x: x["sort_order"]):
    print(f"    第{p['sort_order']}期 {p['name']} | {p['planned_amount']:,.2f} 元 | {p['planned_date']}")
    total += p["planned_amount"]
print(f"    合计 {total:,.2f} 元（应 = 年租金 540,000）")
ok = len(plans) == 4 and abs(total - 540000.0) < 0.01
print("✅ 回归通过" if ok else "❌ 回归失败")
sys.exit(0 if ok else 2)
