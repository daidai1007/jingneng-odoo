"""Audit both local demos and remove the old demo's identified customer fixtures."""

import argparse
import ast
from collections import Counter
from html import unescape
import json
import os
from pathlib import Path
import re
from runpy import run_path
import subprocess
import sys
from xml.etree import ElementTree


WORKSPACE = Path(__file__).resolve().parents[1]
OLD_ROOT = Path(r"C:\Users\Administrator\AppData\Local\OdooNativePreview")
NEW_ROOT = WORKSPACE / "隔离验收"
OLD_DB = "odoo_customer_demo"
NEW_DB = "odoo_jingneng_demo"


def output(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def fixture_records(env, addons):
    records = {}
    modules = env["ir.module.module"].search([("state", "=", "installed")])
    for module in modules.mapped("name"):
        manifest = addons / module / "__manifest__.py"
        if not manifest.is_file():
            continue
        data = ast.literal_eval(manifest.read_text(encoding="utf-8"))
        for source in data.get("demo", ()):
            path = manifest.parent / source
            if path.suffix != ".xml" or not path.is_file():
                continue
            contents = ElementTree.parse(path)
            for node in contents.iter("record"):
                name = node.get("id", "")
                if node.get("model") != "res.partner" or not name or "." in name or node.get("forcecreate") == "0":
                    continue
                xmlid = f"{module}.{name}"
                if xmlid in {"base.res_partner_main1", "base.res_partner_main2"}:
                    continue
                partner = env.ref(xmlid, raise_if_not_found=False)
                if partner and partner.exists() and partner._name == "res.partner":
                    records[partner.id] = xmlid
    return records


def sales_state(env):
    fields = ["name", "state", "amount_total", "partner_id", "partner_invoice_id", "partner_shipping_id"]
    return env["sale.order"].search([], order="id").read(fields)


def business_references(env, candidates):
    models = {
        "sale.order": ("partner_id", "partner_invoice_id", "partner_shipping_id"),
        "purchase.order": ("partner_id", "dest_address_id"),
        "stock.picking": ("partner_id",),
        "stock.move": ("partner_id",),
        "account.move": ("partner_id", "partner_shipping_id", "commercial_partner_id"),
        "account.move.line": ("partner_id",),
        "res.users": ("partner_id",),
        "res.company": ("partner_id",),
    }
    references = set()
    for model, field_names in models.items():
        if model not in env:
            continue
        for field in field_names:
            if field in env[model]._fields:
                references.update(env[model].with_context(active_test=False)
                                  .search([(field, "in", candidates.ids)]).mapped(field).ids)
    return references


def process_old(env, apply):
    from psycopg2 import IntegrityError
    from odoo.exceptions import UserError

    if env.cr.dbname != OLD_DB or env.ref("product.product_product_4", raise_if_not_found=False) is None:
        raise RuntimeError("Expected the old Odoo demonstration database")

    ids = fixture_records(env, OLD_ROOT / "runtime" / "server" / "odoo" / "addons")
    candidates = env["res.partner"].with_context(active_test=False).browse(ids)
    users = env["res.users"].with_context(active_test=False).search([]).partner_id
    companies = env["res.company"].with_context(active_test=False).search([]).partner_id
    candidates -= users | companies
    keep = business_references(env, candidates)
    for partner in candidates:
        if partner.child_ids - candidates:
            keep.add(partner.id)

    summary = {
        "database": OLD_DB,
        "candidates": len(candidates),
        "active_before": len(candidates.filtered("active")),
        "candidate_names": [p.name for p in candidates if not p.parent_id],
        "referenced": len(keep),
        "sales_before": len(sales_state(env)),
    }
    if not apply:
        output(summary)
        return

    if len(ids) == 0 or not {"base.res_partner_1", "base.res_partner_3", "base.res_partner_18"} <= set(ids.values()):
        raise RuntimeError("Expected known demo fixtures before applying changes")
    if (candidates & (users | companies)) or any(p.ref and p.ref.startswith("AIQUOTE-") for p in candidates):
        raise RuntimeError("Unexpected protected record in the old cleanup set")

    before = sales_state(env)
    deleted = []
    archived = []
    for partner in sorted(candidates, key=lambda p: (bool(p.parent_id), p.id), reverse=True):
        if not partner.exists():
            continue
        partner_id = partner.id
        if partner_id not in keep and not partner.with_context(active_test=False).child_ids:
            try:
                with env.cr.savepoint():
                    partner.unlink()
            except (IntegrityError, UserError):
                pass
            else:
                deleted.append(ids[partner_id])
                continue
        if partner.exists() and partner.active:
            partner.with_context(tracking_disable=True).write({"active": False})
            archived.append(ids[partner_id])

    active = candidates.exists().filtered("active")
    if active or before != sales_state(env):
        raise RuntimeError("Default customers remain active or a sale changed; cleanup rolled back")
    env.cr.commit()
    summary.update(deleted=len(deleted), archived=len(archived), active_after=len(active), sales_preserved=len(before))
    output(summary)


def audit_new(env):
    if env.cr.dbname != NEW_DB:
        raise RuntimeError("Expected the separate Jingneng database")

    fixtures = fixture_records(env, NEW_ROOT / "runtime" / "server" / "odoo" / "addons")
    verifier = run_path(str(WORKSPACE / "私有导入" / "替换演示报价.py"))
    demo = run_path(str(NEW_ROOT / "runtime" / "bootstrap.py"), run_name="jingneng_real_quote_audit")
    env = demo["demo_environment"](env)
    cases = json.loads((WORKSPACE / "私有导入" / "现行已报价单.json").read_text(encoding="utf-8"))["quotes"]
    expected = {verifier["ref_for"](case): case["company"] for case in cases}
    orders = env["sale.order"].search([("client_order_ref", "=like", "AIQUOTE:%")])
    actual = {order.client_order_ref: order.partner_id.name for order in orders}
    mismatches = Counter(
        ref in actual for ref, name in expected.items() if actual.get(ref) != name
    )
    if expected != actual or len(orders) != len(expected) or fixtures:
        raise RuntimeError(f"Imported customer mapping mismatch or default partners present ({dict(mismatches)}, {len(fixtures)})")
    units = {line["unit"] for case in cases for line in case["lines"]}
    uom = {}
    for unit in units:
        found = env["uom.uom"].search([("name", "=", unit)])
        if len(found) != 1:
            raise RuntimeError("Imported unit does not resolve uniquely")
        uom[unit] = found
    by_ref = {order.client_order_ref: order for order in orders}
    def note_text(markup):
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", str(markup))).split())

    for case in cases:
        order = by_ref[verifier["ref_for"](case)]
        verifier["verify_order"](order, case, uom)
        if note_text(order.note) != note_text(verifier["order_note"](case)):
            raise RuntimeError("Imported source version or formula details differ")
    output({"database": NEW_DB, "imported_quotes": len(orders), "company_count": len(set(actual.values())),
            "exact_customer_matches": len(actual), "default_partner_count": len(fixtures),
            "verified_lines": sum(len(case["lines"]) for case in cases), "amounts_and_both_methods_verified": True})


def run_shell(target, apply):
    root, database, ports = (OLD_ROOT, OLD_DB, (8069, 15432)) if target == "old" else (NEW_ROOT, NEW_DB, (8070, 15433))
    launcher = run_path(str(root / "runtime" / "manage.py"))
    settings = json.loads((root / "设置.json").read_text(encoding="utf-8"))
    if launcher["DATABASE"] != database or tuple(settings[key] for key in ("http_port", "pg_port")) != ports:
        raise RuntimeError("The local demo's identity or ports changed")
    python = root / "runtime" / "python" / "python.exe"
    odoo_bin = root / "runtime" / "server" / "odoo-bin"
    environment = launcher["child_environment"](settings)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # Both runtime managers derive their Python path from the current process.
    environment["PATH"] = os.pathsep.join((str(python.parent), environment["PATH"]))
    script = Path(__file__).resolve()
    entry = "process_old" if target == "old" else "audit_new"
    arguments = f"env, {apply!r}" if target == "old" else "env"
    code = f"from runpy import run_path\nrun_path({str(script)!r})[{entry!r}]({arguments})\n"
    result = subprocess.run([str(python), str(odoo_bin), "shell", "-c", str(root / "odoo.conf"),
                             "-d", database, "--no-http", "--log-level=warn"],
                            cwd=root, env=environment, input=code.encode("utf-8"), capture_output=True, check=False)
    sys.stdout.write(result.stdout.decode("utf-8", errors="replace"))
    if result.returncode:
        sys.stderr.write(result.stderr.decode("utf-8", errors="replace")[-3600:])
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=["old", "new"])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.target != "old" and args.apply:
        parser.error("--apply is only supported for the old demonstration database")
    run_shell(args.target, args.apply)
