"""Read official AI Quote snapshots through the account's ordinary API."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request


HERE = Path(__file__).resolve().parent
CLIENT_CONFIG = Path(r"G:\gongsi\banjinxitong\板件后续二次修改\AIQuoteDualSystem\client_config.json")
ACCOUNT = "戴晨丽"
FORMAL_STATUSES = {"QUOTED", "CONTRACT", "ARCHIVED", "REVISING"}
CENT = Decimal("0.01")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class QuoteClient:
    def __init__(self):
        config = json.loads(CLIENT_CONFIG.read_text(encoding="utf-8-sig"))
        url = urlsplit(config["api_url"])
        if (url.scheme, url.netloc, url.path) != (
            "https", "ai-quote-dual-test.onrender.com", "/api/quotes/calculate-dual"
        ):
            raise RuntimeError("The approved quote endpoint changed")
        self.origin = f"{url.scheme}://{url.netloc}"
        self.headers = {"X-AI-Quote-Key": config["api_key"], "Content-Type": "application/json"}
        self.opener = build_opener(NoRedirect())

    def request(self, path, login_payload=None):
        body = None if login_payload is None else json.dumps(login_payload, ensure_ascii=False).encode()
        request = Request(self.origin + path, data=body, headers=self.headers)
        try:
            with self.opener.open(request, timeout=45) as response:
                return json.load(response, parse_float=Decimal)
        except HTTPError as error:
            raise RuntimeError(f"Quote API HTTP {error.code}") from None

    def __enter__(self):
        session = self.request("/api/name-login", {"name": ACCOUNT})
        if session["person"]["display_name"] != ACCOUNT:
            raise RuntimeError("The quote account differs from the authorized account")
        self.headers["Authorization"] = "Bearer " + session["token"]
        return self

    def __exit__(self, *_error):
        self.request("/api/name-logout", {})

    def list_quotes(self):
        rows = []
        page = 1
        while True:
            data = self.request(f"/api/quote-schemes?page_size=100&page={page}")
            rows.extend(data["items"])
            if len(rows) >= data["total"]:
                if len({row["scheme_no"] for row in rows}) != len(rows) or len(rows) != data["total"]:
                    raise RuntimeError("Visible quote list changed during paging")
                return rows
            if not data["items"]:
                raise RuntimeError("Quote list was cut short")
            page += 1

    def detail(self, number):
        return self.request("/api/quote-schemes/" + quote(number, safe=""))


def name_valid(name):
    return bool(name and name.casefold() not in {"无", "未填写", "未知", "null", "none", "-"}
                and not any(word in name for word in ("测试", "合成", "演示", "样例")))


def money(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or amount.quantize(CENT) != amount:
        raise ValueError("invalid_amount")
    return amount.quantize(CENT)


def lines_from(items):
    if not items:
        raise ValueError("missing_formal_lines")
    result = []
    for item in items:
        row = item.get("quote_display") or {}
        name, spec, unit = (str(row.get(field) or "").strip() for field in ("name", "specification", "unit"))
        qty = Decimal(str(row.get("quantity") or 0))
        if not name or not spec or not unit or qty < 1 or qty != qty.to_integral_value():
            raise ValueError("missing_formal_line_fields")
        quick_unit, quick_total, formula_unit, formula_total = (
            money(row[field]) for field in ("unit_price", "total", "formula_unit_price", "formula_total")
        )
        if quick_unit * qty != quick_total or formula_unit * qty != formula_total:
            raise ValueError("unit_and_line_amount_differ")
        result.append({"name": name, "specification": spec, "quantity": int(qty), "unit": unit,
                       "quick_unit": str(quick_unit), "quick_total": str(quick_total),
                       "formula_unit": str(formula_unit), "formula_total": str(formula_total)})
    return result


def normalize(number, version, name, date, items, approved_total):
    if not name_valid(name):
        raise ValueError("missing_customer")
    lines = lines_from(items)
    total = sum(Decimal(line["quick_total"]) for line in lines)
    if total <= 0 or total != money(approved_total):
        raise ValueError("quote_total_differs")
    return {"scheme_no": number, "version": version, "company": name, "quote_date": date,
            "method": "QUICK", "total": str(total), "lines": lines}


def formal_versions(header, detail):
    if (header["lock_version"], header["status"], header["scheme_no"]) != (
        detail["lock_version"], detail["status"], detail["scheme_no"]
    ):
        raise ValueError("changed_during_read")

    snapshot = detail["snapshot"]
    module = snapshot.get("quote_management") or {}
    current = module.get("current") or detail["version_no"]
    doc = snapshot.get("document") or {}
    frozen = snapshot.get("contract_quote_snapshot") or {}
    revising = detail["status"] == "REVISING"
    if not revising and current == header["quote_version"] and frozen.get("schema") == 1 and frozen.get("items"):
        name = (snapshot.get("company") or "").strip()
        if name == (header.get("company_name") or "").strip() and header["quoted_amount"] == detail["quoted_amount"]:
            yield current, name, doc.get("quote_date"), frozen["items"], detail["quoted_amount"], "formal_snapshot"

    for version in sorted(module.get("versions") or (), key=lambda item: item.get("version") or 0, reverse=True):
        number = version.get("version")
        if (not isinstance(number, int) or number < 1 or version.get("revision_draft")
                or version.get("pending_confirmation") or not (version.get("confirmed") or version.get("sealed"))
                or (revising and number >= current)):
            continue
        name = (version.get("company") or "").strip()
        if number == current and (name != (header.get("company_name") or "").strip()
                                  or version.get("amount") != detail["quoted_amount"]):
            continue
        yield number, name, version.get("quote_date") or doc.get("quote_date"), version.get("items"), version.get("amount"), "confirmed_version"


def inspect_quote(client, header, existing):
    number = header["scheme_no"]
    detail = client.detail(number)
    reasons = Counter()
    for version, company, date, items, total, provenance in formal_versions(header, detail):
        if existing and version != existing["version"]:
            continue
        try:
            case = normalize(number, version, company, date, items, total)
        except (KeyError, InvalidOperation, TypeError):
            reasons["missing_formal_amount_fields"] += 1
        except ValueError as failure:
            reasons[str(failure)] += 1
        else:
            if existing:
                # A version's older archive may omit its date. The imported commercial fields must still match.
                columns = set(case) - {"quote_date"}
                if all(case[field] == existing[field] for field in columns):
                    return "verified", None, provenance
                reasons["snapshot_fields_differ"] += 1
            else:
                return "candidate", case, provenance
    return "excluded", None, reasons.most_common(1)[0][0] if reasons else "no_complete_confirmed_snapshot"


def main():
    old = json.loads((HERE / "现行已报价单.json").read_text(encoding="utf-8"))["quotes"]
    existing = {case["scheme_no"]: case for case in old}
    with QuoteClient() as client:
        visible = client.list_quotes()
        rows = [row for row in visible if row["status"] in FORMAL_STATUSES and row["source_kind"] == "QUOTE"]
        contracts = [row for row in visible if row["status"] == "CONTRACT" and row["source_kind"] != "QUOTE"]
        original_by_no = {row["scheme_no"]: row for row in visible if row["source_kind"] == "QUOTE"}
        links = Counter()
        for contract in contracts:
            source = contract.get("source_scheme_no")
            if source in existing:
                links["original_already_imported"] += 1
            elif source in original_by_no:
                links["original_" + original_by_no[source]["status"].lower()] += 1
            else:
                links["no_visible_original"] += 1
        print(json.dumps({"visible_by_status": Counter(row["status"] for row in visible),
                          "formal_quote_records_by_status": Counter(row["status"] for row in rows)}), flush=True)
        work = []
        rejected = Counter()
        for row in rows:
            if row["scheme_no"] in existing or name_valid((row.get("company_name") or "").strip()):
                work.append(row)
            else:
                rejected[f"{row['status']}:missing_customer"] += 1
        verified = 0
        failed_existing = Counter()
        candidates = []
        candidate_sources = Counter()
        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [workers.submit(inspect_quote, client, row, existing.get(row["scheme_no"])) for row in work]
            for row, future in zip(work, futures):
                try:
                    status, case, reason = future.result()
                except ValueError as failure:
                    status, case, reason = "excluded", None, str(failure)
                if status == "verified":
                    verified += 1
                elif status == "candidate":
                    candidates.append(case)
                    candidate_sources[f"{row['status']}:{reason}"] += 1
                elif row["scheme_no"] in existing:
                    failed_existing[reason] += 1
                else:
                    rejected[f"{row['status']}:{reason}"] += 1

    selected_numbers = {row["scheme_no"] for row in work}
    if missing := existing.keys() - selected_numbers:
        failed_existing["no_longer_visible_as_formal_quote"] = len(missing)
    summary = {
        "visible_by_status": dict(Counter(row["status"] for row in visible)),
        "formal_quote_records_by_status": dict(Counter(row["status"] for row in rows)),
        "contract_origin_check": dict(links),
        "verified_existing": verified, "existing_issues": dict(failed_existing),
        "additional_candidates": len(candidates), "candidate_sources": dict(candidate_sources),
        "excluded_additional": dict(rejected), "needed_to_reach_50": 50 - len(old),
    }
    (HERE / "正式报价复核统计.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not failed_existing and len(candidates) >= 50 - len(old):
        (HERE / "可补录正式报价.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
