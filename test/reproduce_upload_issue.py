# 复现手动上传超时问题：XML-RPC 模拟向导 action_create_and_extract
import base64
import sys
import time
import xmlrpc.client

URL = "http://localhost:8069"
DB = "contract_db"
USER = "admin"
PWD = "admin"

pdf_path = sys.argv[1]
common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
uid = common.authenticate(DB, USER, PWD, {})
if not uid:
    print("LOGIN FAILED")
    sys.exit(1)
print(f"login ok uid={uid}")

models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")

with open(pdf_path, "rb") as f:
    pdf_b64 = base64.b64encode(f.read()).decode()

import os
filename = os.path.basename(pdf_path)

wizard_id = models.execute_kw(DB, uid, PWD, "contract.upload.wizard", "create", [{
    "pdf_file": pdf_b64,
    "pdf_filename": filename,
}])
print(f"wizard created id={wizard_id}, calling action_create_and_extract ...")
t0 = time.time()
try:
    result = models.execute_kw(DB, uid, PWD, "contract.upload.wizard",
                               "action_create_and_extract", [[wizard_id]])
    elapsed = time.time() - t0
    print(f"action returned in {elapsed:.1f}s -> {result}")
except Exception as e:
    elapsed = time.time() - t0
    print(f"action FAILED after {elapsed:.1f}s: {type(e).__name__}: {e}")

# 查看最新合同状态
ids = models.execute_kw(DB, uid, PWD, "contract.contract", "search",
                        [[], {"order": "create_date desc", "limit": 3}])
recs = models.execute_kw(DB, uid, PWD, "contract.contract", "read", [ids],
                         {"fields": ["name", "code", "state", "type", "amount",
                                     "date_signed", "is_ai_generated", "extraction_confidence"]})
print("--- latest contracts ---")
for r in recs:
    print(r)
