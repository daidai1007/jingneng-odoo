
"""Replace only the original fictional quotation fixtures in the local demo."""
from collections import Counter
from decimal import Decimal
from hashlib import sha256
from html import escape
from pathlib import Path
from runpy import run_path
import json
import re

MARKER = "odoo_jingneng_preview.real_quote_cases_imported"

def checked(condition, message):
    if not condition:
        raise RuntimeError(message)

def ref_for(case):
    return f"AIQUOTE:{case['scheme_no']}/V{case['version']}"

def case_description(item):
    return f"{item['name']}\n规格：{item['specification']}"

def order_note(case):
    total = sum(Decimal(line['formula_total']) for line in case['lines'])
    headline = f"来源：{ref_for(case)}。报价日期：{case['quote_date'] or '未记录'}。采用快速报价，原单合计 ¥{case['total']}。"
    compare = f"同一报价原公式法合计 ¥{total:.2f}。"
    rows = [
        escape(f"{i}．{line['name']} · {line['specification']} · {line['quantity']}{line['unit']} · 单价 ¥{line['formula_unit']} · 小计 ¥{line['formula_total']}")
        for i, line in enumerate(case['lines'], 1)
    ]
    return '<p>' + escape(headline) + '</p><p>' + escape(compare) + '</p><p>' + '<br/>'.join(rows) + '</p>'

def verify_order(order, case, uom):
    checked(order.state == "draft" and not order.picking_ids and not order.invoice_ids, "Imported order is not an independent draft")
    checked(order.partner_id.name == case['company'] and order.currency_id.name == "CNY", "Wrong customer or currency")
    checked(Decimal(str(order.amount_total)) == Decimal(case['total']), "Wrong order total")
    checked(len(order.order_line) == len(case['lines']), "Wrong line count")
    for actual, expected in zip(order.order_line, case['lines']):
        checked(actual.name == case_description(expected) and actual.product_uom_qty == expected['quantity']
                and actual.product_uom_id == uom[expected['unit']]
                and Decimal(str(actual.price_unit)) == Decimal(expected['quick_unit'])
                and Decimal(str(actual.price_total)) == Decimal(expected['quick_total'])
                and not actual.tax_ids, "Wrong original quote line")
    checked("采用快速报价" in str(order.note) and "原公式法合计" in str(order.note), "Pricing method evidence is missing")
    return dict(reference=order.client_order_ref, number=order.name, state=order.state,
                customer_id=order.partner_id.id, total_cny=case['total'])

def replace(source_env, root, apply):
    from odoo import Command

    runtime = root / "隔离验收" / "runtime"
    demo = run_path(str(runtime / "bootstrap.py"), run_name="jingneng_real_quote_import")
    env = demo["demo_environment"](source_env)
    env.cr.execute("SELECT pg_try_advisory_xact_lock(%s)", (2026111750,))
    checked(env.cr.fetchone()[0], "Another demo import or initialization is running")
    company = env.ref("base.main_company")
    admin = env.ref("base.user_admin")
    warehouse = env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
    category = env["product.category"].search([("name", "=", demo["COMPANY"])], limit=2)
    checked(company.currency_id.name == "CNY" and warehouse and len(category) == 1, "Unexpected Odoo demo setup")
    cases = json.loads((root / "私有导入" / "现行已报价单.json").read_text(encoding="utf-8"))['quotes']
    checked(1 <= len(cases) <= 50 and len({ref_for(case) for case in cases}) == len(cases), "Wrong selection set")
    for case in cases:
        checked(case['method'] == 'QUICK' and re.fullmatch(r'BJ-[0-9]{8}-[0-9]+', case['scheme_no'], re.I)
                and isinstance(case['version'], int) and case['version'] >= 1
                and len(case['lines']) > 0 and all(re.fullmatch(r'[\w\u4e00-\u9fff]{1,10}', item['unit']) for item in case['lines']),
                "Invalid original quote identity or unit")
    model = env["sale.order"].with_company(company)
    company_domain = [("company_id", "=", company.id)]
    refs = [ref_for(case) for case in cases]
    markers = env["ir.config_parameter"]
    fictional_cases = [case for case in demo["CASES"] if not case["confirmed"]]
    fictional_cases += demo["quote_case_data"](runtime / "bootstrap.py")["make_cases"]()
    fake_mapping = {case["ref"]: case for case in fictional_cases}
    old = model.search(company_domain + [("client_order_ref", "in", list(fake_mapping))])
    existing_real = model.search(company_domain + [("client_order_ref", "in", refs)])
    if markers.get_bool(MARKER):
        checked(not old and len(existing_real) == len(cases), "Imported set has changed. Review before repeating.")
    else:
        checked(not existing_real and set(old.mapped("client_order_ref")) == set(fake_mapping)
                and len(old) == len(fake_mapping) == 50, "Fictional drafts cannot be identified uniquely")
        for order in old:
            checked(not order.picking_ids and not order.invoice_ids and not order.transaction_ids, "A fictional draft has downstream records")
            demo["verify_order"](order, fake_mapping[order.client_order_ref])
            checked(order.state == "draft", "A prior demo quote is no longer a draft")
    retained = model.search(company_domain + [("id", "not in", (old | existing_real).ids)], order="id")
    fields = ["name", "client_order_ref", "state", "partner_id", "amount_total", "write_date", "picking_ids", "order_line"]
    retained_before = retained.read(fields)
    checked(any(row['client_order_ref'] == 'JN-DEMO-SO-READY' and row['state'] == 'sale'
                for row in retained_before), "Confirmed demo order is missing")
    units = sorted({line['unit'] for case in cases for line in case['lines']})
    uom = {}
    products = {}
    for unit in units:
        unit_ref = env.ref("uom.product_uom_unit")
        matching = env["uom.uom"].search([("name", "=", unit)], limit=2)
        checked(len(matching) <= 1, "Ambiguous unit")
        uom[unit] = matching or env["uom.uom"].create({"name":unit,"relative_uom_id":unit_ref.id,"relative_factor":1})
        checked(uom[unit].factor == unit_ref.factor, "Existing unit has a different conversion factor")
        code = "AIQUOTE-IMPORT-" + sha256(unit.encode()).hexdigest()[:12].upper()
        products[unit] = env["product.product"].with_company(company).search([('default_code','=',''+code),('company_id','=',company.id)], limit=2)
        checked(len(products[unit]) <= 1, "Duplicate demonstration import product")
        if not products[unit]:
            products[unit] = env["product.product"].with_company(company).create({
                "name": "现行报价明细（" + unit + "）", "default_code": code, "sale_ok": True,
                "purchase_ok": False, "type": "service", "company_id": company.id, "categ_id": category.id,
                "uom_id": uom[unit].id, "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()],
            })
    customers = {}
    for name in sorted({case['company'] for case in cases}):
        customer_ref = "AIQUOTE-CUSTOMER-" + sha256(name.encode()).hexdigest()[:18].upper()
        partners = env["res.partner"].with_company(company)
        own = partners.search([("ref", "=", customer_ref)], limit=2)
        checked(len(own) <= 1 and (not own or own.name == name), "Customer identity is ambiguous")
        customers[name] = own or partners.create({"name": name, "ref": customer_ref, "country_id": company.country_id.id})
    if not existing_real:
        for case in cases:
            lines = [
                Command.create({"product_id": products[line["unit"]].id, "product_uom_id": uom[line["unit"]].id,
                    "name": case_description(line), "product_uom_qty": line["quantity"],
                    "price_unit": float(Decimal(line["quick_unit"])), "discount": 0, "tax_ids": [Command.clear()]})
                for line in case['lines']
            ]
            existing_real |= model.create({"partner_id": customers[case['company']].id, "company_id": company.id,
                                           "warehouse_id": warehouse.id, "user_id": admin.id, "client_order_ref":ref_for(case),
                                           "order_line":lines, "note":order_note(case)})
    by_ref = {order.client_order_ref: order for order in existing_real}
    fresh = [verify_order(by_ref[ref_for(case)], case, uom) for case in cases]
    deleted = len(old)
    if old:
        old.unlink()
    checked(retained.read(fields) == retained_before, "Other sales records changed")
    checked(not model.search_count(company_domain + [("client_order_ref", "in", list(fake_mapping))]), "Fictional drafts still exist")
    markers.set_bool(MARKER, True)
    if apply:
        env.cr.commit()
    else:
        env.cr.rollback()
    print("REAL_QUOTE_ORM=" + json.dumps({"documents":fresh, "count":len(fresh),
        "companies":len(customers), "lines":sum(len(case['lines']) for case in cases), "fictional_removed":deleted,
        "other_orders_preserved":len(retained_before), "committed":apply}), flush=True)

