# Part of Odoo. See LICENSE file for full copyright and licensing details.
# ruff: noqa: PLW0642

import json
import math
import re
import unicodedata
import logging
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import timedelta
from tempfile import TemporaryDirectory
from io import BytesIO
from hashlib import sha256
from itertools import groupby
from pathlib import Path, PurePath
from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Command, Domain
from odoo.http import request
from odoo.tools import SQL, OrderedSet, float_is_zero, float_round, format_amount, is_html_empty
from odoo.tools.mail import html_keep_url
from odoo.tools.urls import urljoin

from odoo.addons.payment import utils as payment_utils

_logger = logging.getLogger(__name__)

_SELECTION_PRICING_WORKER = "import ast, builtins, copy, hashlib, importlib, json, math, symtable, sys\nfrom pathlib import Path\nsys.dont_write_bytecode = True\nsource = Path(sys.argv[1])\nsys.path.insert(0, str(source))\ndata = json.load(sys.stdin)\n\nclass OriginalFunctions:\n    # Reuse only requested non-UI functions from the mixed client modules.\n    # No Qt import, application construction, or replacement pricing formula.\n    def __init__(self, name):\n        self.path = source / (name + '.py')\n        self.text = self.path.read_text(encoding='utf-8-sig')\n        self.nodes = {}\n        self.prior = {}\n        self.ns = {'__name__': 'odoo_quote_original_functions', '__builtins__': builtins.__dict__}\n        body = ast.parse(self.text).body\n        nodes = [child for node in body for child in (node.body if isinstance(node, ast.Try) else [node])]\n        for node in nodes:\n            names = []\n            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):\n                names = [node.name]\n            elif isinstance(node, ast.Assign):\n                names = [n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)]\n            elif isinstance(node, ast.ImportFrom):\n                names = [n.asname or n.name for n in node.names]\n            elif isinstance(node, ast.Import):\n                names = [n.asname or n.name.split('.')[0] for n in node.names]\n            for key in names:\n                if key in self.nodes:\n                    self.prior[key] = self.nodes[key]\n                self.nodes[key] = node\n        self.loading = set()\n    def get(self, name):\n        if name in self.ns:\n            return self.ns[name]\n        if hasattr(builtins, name):\n            return getattr(builtins, name)\n        if name in self.loading:\n            return None\n        node = self.nodes.get(name)\n        if node is None:\n            raise ValueError('原规则依赖缺失：' + name)\n        if isinstance(node, ast.ClassDef):\n            raise ValueError('计价路径不允许加载界面类：' + name)\n        if isinstance(node, ast.ImportFrom) and (node.module or '').startswith(('PySide6', 'shiboken6')):\n            raise ValueError('计价路径不允许加载界面依赖')\n        if isinstance(node, ast.Assign) and name in self.prior and any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == name for n in ast.walk(node.value)):\n            self.nodes[name] = self.prior[name]\n            self.get(name)\n            self.nodes[name] = node\n        self.loading.add(name)\n        if not isinstance(node, (ast.Import, ast.ImportFrom)):\n            table = symtable.symtable(ast.unparse(node), str(self.path), 'exec')\n            deps = set()\n            def visit(table):\n                for symbol in table.get_symbols():\n                    if symbol.is_referenced() and symbol.is_global():\n                        deps.add(symbol.get_name())\n                for child in table.get_children():\n                    visit(child)\n            visit(table)\n            for dependency in deps - {name}:\n                if dependency not in self.ns and not hasattr(builtins, dependency):\n                    self.get(dependency)\n        unit = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])\n        ast.fix_missing_locations(unit)\n        exec(compile(unit, str(self.path), 'exec'), self.ns)\n        self.loading.remove(name)\n        return self.ns[name]\n\noperation = data['operation']\nif operation == 'defaults':\n    original = OriginalFunctions('scheme2_ui')\n    document = ast.parse(original.text)\n    definition = next(node for node in ast.walk(document) if isinstance(node, ast.Assign)\n                      and any(isinstance(target, ast.Attribute) and target.attr == 'scheme2_defaults' for target in node.targets))\n    if not isinstance(definition.value, ast.Dict):\n        raise ValueError('原程序默认计价参数结构已变化，请核对版本')\n    names = {node.id for node in ast.walk(definition.value) if isinstance(node, ast.Name)}\n    for name in names:\n        original.get(name)\n    output = eval(compile(ast.Expression(body=definition.value), str(original.path), 'eval'), original.ns)\n    surfaces = original.get('SURFACE_DEFAULT_PRICES')\n    output['surface_prices'] = {key: surfaces[label] for key, label in [('orange', '橘纹'), ('gloss', '平光'), ('none', '无')]}\nelif operation == 'match':\n    original = OriginalFunctions('attachment_v2_client')\n    match = original.get('match_catalog_attachment')\n    selected_input = original.get('selected_input')\n    selected, explanations = [], []\n    for allocation in data['allocations']:\n        if not allocation.get('selected', True):\n            continue\n        index = allocation.get('cabinet_index')\n        if allocation.get('scope') == 'cabinet':\n            if type(index) is not int or index < 0 or index >= len(data['cabinets']):\n                raise ValueError('附件柜位无效：' + allocation['name'])\n            target = data['cabinets'][index]\n        else:\n            target = data['environment']\n        family = target['product_code'].split('_')[0]\n        box_products = OriginalFunctions('scheme2_ui').get('CONTROL_BOX_ATTACHMENT_PRODUCTS')\n        choices = [row for row in data['catalog']['items'] if row['item_name'] == allocation['name']\n                   and not (row.get('category_level1') == '控制箱附件' and family not in box_products)\n                   and not (row.get('category_level1') == '控制柜附件' and family not in {'JS', 'JP', 'JM'})]\n        categories = {row.get('category_level1', '') for row in choices}\n        if len(categories) > 1 and not allocation.get('category_level1'):\n            raise ValueError('附件有多个适用分类，请明确分类后计价：' + allocation['name'])\n        selection = {'item_name': allocation['name'], 'category_level1': allocation.get('category_level1') or (next(iter(categories)) if len(categories) == 1 else ''),\n                     'quantity': allocation['quantity'], 'attachment_price_sign': 1,\n                     'manual_inputs': allocation.get('manual_inputs') or {}}\n        if allocation.get('model_code'):\n            selection['model_code'] = allocation['model_code']\n        dimensions = tuple(target[key] for key in ('width_mm', 'height_mm', 'depth_mm'))\n        found = match(selection, data['catalog']['items'], dimensions, target['product_code'],\n                      target.get('base_height_mm'), target['material_code'])\n        if not found:\n            raise ValueError('附件未匹配到唯一有效规格，请完善规格/型号：' + allocation['name'])\n        if original.get('installation_board')(selection) or original.get('is_base_attachment')(selection):\n            resolved = original.get('resolve_installation_board')(selection, found, data['catalog']['data_version'])\n        elif original.get('size_match_attachment_name')(selection):\n            resolved = original.get('resolve_size_matched_attachment')(selection, found, data['catalog']['data_version'])\n        else:\n            resolved = {**found, **selection, 'catalog_version': data['catalog']['data_version']}\n        if allocation.get('scope') == 'cabinet' and len(data['cabinets']) > 1:\n            resolved['ganged_cabinet_index'] = index\n        selected.append(selected_input(resolved, target.get('base_height_mm')))\n        explanations.append(resolved)\n    output = {'selected': selected, 'matched': explanations}\nelif operation == 'finish':\n    original = OriginalFunctions('scheme2_ui')\n    item = data['item']\n    response = data['response']\n    item.update(formula=copy.deepcopy(response['formula_cost']), quick=copy.deepcopy(response['quick_quote']),\n                attachments=copy.deepcopy(response.get('attachments') or []),\n                quote_line_id=response.get('quote_line_id'), attachment_contract=2,\n                manufacturing_cost_basis='quoted_unit_4pct_v1', tax_cost_basis='quoted_less_display_costs_15pct_v1',\n                gross_margin_basis='production_plus_cost_freight_v1')\n    # The server owns formula prices. Only validated size-generated sale overrides\n    # from the original matcher are merged, using the original client function.\n    matcher = OriginalFunctions('attachment_v2_client')\n    if len(item['attachments']) != len(data['matched']):\n        raise ValueError('附件计算结果数量与选择不一致')\n    remaining = list(item['attachments'])\n    merged = []\n    for selected in data['matched']:\n        costs = [row for row in remaining if row.get('attachment_price_id') == selected.get('attachment_price_id')\n                 and row.get('ganged_cabinet_index') == selected.get('ganged_cabinet_index')\n                 and float(row.get('quantity', 1)) == float(selected.get('quantity', 1))]\n        if not costs:\n            raise ValueError('附件结果与匹配规格不一致：' + str(selected.get('item_name', '')))\n        cost = costs[0]\n        remaining.remove(cost)\n        merged.append(matcher.get('merge_cost')(selected, cost))\n    item['attachments'] = merged\n    if item.get('freight_mode') == 'AUTO':\n        import freight_state\n        item['freight_basis'] = freight_state.AUTO_FREIGHT_BASIS\n        freight_state.sync_item_freight(item)\n    original.get('_recalculate_item_management')(item)\n    if item['formula'].get('tax_cost_error') or item['formula'].get('total_cost') is None:\n        raise ValueError(item['formula'].get('tax_cost_error') or '成本结果不完整')\n    values = original.get('_row_values')(item)\n    headings = original.get('HEADERS')\n    order = original.get('COST_DISPLAY_ORDER')\n    money_columns = original.get('MONEY_COLUMNS')\n    frozen = {heading: (original.get('_money')(values[order[column]]) if column in money_columns else str(values[order[column]]))\n              for column, heading in enumerate(headings)}\n    item['quote_cost_display'] = {'schema': 1, 'values': frozen,\n                                 'price_breakdown': original.get('_cost_price_components')(item)}\n    item['quote_display'] = {'unit_price': values[15], 'total': values[16], 'quantity': item['quantity']}\n    output = {'item': item, 'price_unit': values[15], 'line_total': values[16]}\nelse:\n    raise ValueError('未知计价处理步骤')\noutput['source_sha256'] = hashlib.sha256(original.path.read_bytes()).hexdigest()\nprint(json.dumps(output, ensure_ascii=False, allow_nan=False))\n"
_SELECTION_PRICING_DEFAULT_CACHE = {}

_CABINET_DOORS = ("前单开门后背板", "前双开门后背板", "前后单开门",
                  "前单开门后双开门", "前双开门后单开门", "前后双开门")
_CABINET_DOOR_POSITIONS = (("SINGLE", "PANEL", 1, 0), ("DOUBLE", "PANEL", 0, 1),
                           ("SINGLE", "SINGLE", 2, 0), ("SINGLE", "DOUBLE", 1, 1),
                           ("DOUBLE", "SINGLE", 1, 1), ("DOUBLE", "DOUBLE", 0, 2))
_CABINET_SIZED_ACCESSORIES = {"安装板", "JK安装板", "固定底座", "活动底座", "防雨顶",
                              "分段板", "内门", "玻璃门", "通风顶罩"}


def _cabinet_door_options(series):
    if series in ("JP", "JS", "JP WIDE", "JS WIDE"):
        return _CABINET_DOORS
    return _CABINET_DOORS[:2] if series in ("JA", "JE") else _CABINET_DOORS[:1]


def _normalize_cabinet_values(original):
    """Ordered widths and door positions; a set remains one native sale line."""
    values = dict(original)
    expression = str(values.get("width_expression") or "").strip()
    if not expression and values.get("width_mm"):
        expression = f"{float(values['width_mm']):g}"
    widths = []
    if expression:
        if len(expression) > 1000 or not re.fullmatch(r"\d+(?:\.\d+)?(?:\s*\+\s*\d+(?:\.\d+)?)*", expression):
            raise ValidationError("宽度请填写正数，多个柜位用半角 + 分隔，例如 600+600。")
        widths = [float(part.strip()) for part in expression.split("+")]
        if len(widths) > 20 or any(not math.isfinite(w) or w <= 0 for w in widths) or not math.isfinite(sum(widths)):
            raise ValidationError("每个柜位宽度必须大于零，一组最多支持 20 个柜位。")
    previous = values.get("cabinet_configuration") or {}
    if not isinstance(previous, dict) or previous.get("version", 1) != 1 or not isinstance(previous.get("cabinets", []), list):
        raise ValidationError("柜位配置无效，请重新选择门型。")
    old = previous.get("cabinets", [])
    if len(old) > 20 or any(not isinstance(item, dict) for item in old):
        raise ValidationError("柜位配置无效。")
    allowed = _cabinet_door_options(values.get("product_series"))
    cabinets = []
    for index, width in enumerate(widths):
        prior = old[index] if index < len(old) else {}
        manual = prior.get("door_manual") is True
        door = prior.get("door_type") if manual else None
        if not old and len(widths) == 1 and values.get("door_type") in allowed:
            door, manual = values["door_type"], True
        if manual and door not in allowed:
            raise ValidationError(f"{index + 1}号柜门型不适用于当前产品系列，请重新选择。")
        door = door or (allowed[1] if width > 800 and len(allowed) > 1 else allowed[0])
        front, rear, single, double = _CABINET_DOOR_POSITIONS[_CABINET_DOORS.index(door)]
        cabinets.append({"index": index, "width_mm": width, "door_type": door,
                         "door_manual": manual, "door_position": {"front": front, "rear": rear},
                         "single_door_count": single, "double_door_count": double})
    values.update(width_expression="+".join(f"{w:g}" for w in widths), width_mm=sum(widths),
                  cabinet_configuration={"version": 1, "cabinets": cabinets},
                  door_type=cabinets[0]["door_type"] if cabinets else values.get("door_type") or False)
    basis = {key: values.get(key) for key in ("product_series", "width_expression", "depth_mm", "height_mm",
             "material", "thickness_mm", "coating_type")}
    basis["doors"] = [item["door_type"] for item in cabinets]
    signature = sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    values["cabinet_configuration"]["signature"] = signature
    if values.get("accessory_selection_data"):
        values["accessory_selection_data"] = _cabinet_accessory_allocations(
            values["accessory_selection_data"], cabinets, signature)
    return values


def _cabinet_accessory_allocations(data, cabinets, signature):
    """Assign known sized items to cabinets; never infer arbitrary manual multipliers."""
    if not isinstance(data, dict):
        raise ValidationError("附件选配数据无效。")
    data = dict(data)
    names = set(data.get("choices", {})) | set(data.get("manual_names", []))
    previous = data.get("allocations", [])
    if not isinstance(previous, list) or len(previous) > 1000:
        raise ValidationError("附件柜位配置无效。")
    allocations, existing = [], set()
    for raw in previous:
        if not isinstance(raw, dict) or raw.get("name") not in names:
            raise ValidationError("附件柜位不属于当前附件清单。")
        item = dict(raw)
        scope = item.get("scope")
        index = item.get("cabinet_index")
        if scope not in ("cabinet", "group", "unassigned"):
            raise ValidationError("请为附件指定柜位或整组。")
        if scope == "cabinet" and (type(index) is not int or index < 0):
            raise ValidationError("附件柜号无效。")
        if scope == "cabinet" and index >= len(cabinets):
            if item.get("automatic") and not item.get("quantity_manual"):
                continue
            scope, index = "unassigned", None
        if scope != "cabinet":
            index = None
        quantity = item.get("quantity")
        if len(cabinets) == 1:
            scope, index = "cabinet", 0
            if quantity is None and item.get("scope") == "unassigned" and not item.get("quantity_manual"):
                quantity = 1
        if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, (int, float))
                                     or not math.isfinite(quantity) or quantity <= 0):
            raise ValidationError("附件数量必须是大于零的有效数字。")
        key = (item["name"], scope, index)
        if key in existing:
            if len(cabinets) == 1:
                previous = next(a for a in allocations if a["name"] == item["name"])
                previous["quantity"] = previous["quantity"] + quantity if previous["quantity"] is not None and quantity is not None else None
                previous["selected"] = previous["selected"] or item.get("selected", True)
                previous["quantity_manual"] = previous["quantity_manual"] or item.get("quantity_manual") is True
                continue
            raise ValidationError("同一附件在同一柜位或整组中重复，请合并数量。")
        if type(item.get("selected", True)) is not bool:
            raise ValidationError("附件选择状态无效。")
        existing.add(key)
        manual = item.get("manual_inputs") or {}
        if not isinstance(manual, dict) or len(manual) > 30 or any(not isinstance(k, str) or len(k) > 80 or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for k, v in manual.items()):
            raise ValidationError("附件实际尺寸必须为有效正数。")
        parameters = item.get("manual_parameters") or []
        if not isinstance(parameters, list) or len(parameters) > 30 or any(not isinstance(p, dict) or not isinstance(p.get("name"), str) or len(p["name"]) > 80 for p in parameters):
            raise ValidationError("附件尺寸参数无效，请重新匹配。")
        allocations.append({"manual_inputs": manual, "manual_parameters": parameters,
                            **({"category_level1": item["category_level1"]} if item.get("category_level1") else {}),
                            "name": item["name"], "scope": scope, "cabinet_index": index,
                            "quantity": quantity, "selected": item.get("selected", True),
                            "automatic": item.get("automatic") is True,
                            "quantity_manual": item.get("quantity_manual") is True})
    for name in sorted(names):
        if any(item["name"] == name for item in allocations):
            # Keep explicit object/quantity choices, including deselection.
            if name in _CABINET_SIZED_ACCESSORIES and all(item["automatic"] for item in allocations if item["name"] == name):
                for cabinet in cabinets:
                    key = (name, "cabinet", cabinet["index"])
                    if key not in existing:
                        allocations.append({"name": name, "scope": "cabinet", "cabinet_index": cabinet["index"],
                                            "quantity": 1, "selected": data.get("choices", {}).get(name, True),
                                            "automatic": True, "quantity_manual": False})
            continue
        targets = cabinets if name in _CABINET_SIZED_ACCESSORIES or len(cabinets) == 1 else []
        if targets:
            allocations.extend({"name": name, "scope": "cabinet", "cabinet_index": c["index"],
                                "quantity": 1, "selected": data.get("choices", {}).get(name, True),
                                "automatic": True, "quantity_manual": False} for c in targets)
        else:
            allocations.append({"name": name, "scope": "unassigned", "cabinet_index": None,
                                "quantity": None, "selected": data.get("choices", {}).get(name, True),
                                "automatic": False, "quantity_manual": False})
    data.update(allocations=allocations, configuration_signature=signature)
    return data

_DRAWING_RECOGNITION_WORKER = r'''

import json, os, pathlib, socket, sys, re
payload = json.load(sys.stdin)
os.environ.update(QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1",
                  OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
def no_network(*args, **kwargs):
    raise OSError("Local drawing recognition has no network access")
socket.create_connection = no_network
socket.socket.connect = no_network
client = pathlib.Path(payload["client"])
bundle = pathlib.Path(payload["bundle"])
sys.path.insert(0, str(client))
sys.path.extend([payload["dependencies"], str(bundle), payload["pure_dependencies"]])
handles = [os.add_dll_directory(str(path)) for path in
           (bundle, bundle/"onnxruntime/capi") if path.is_dir()]
os.environ["AI_QUOTE_V3_CORE_ROOT"] = str(client/"v3_core")
os.environ.pop("AIQUOTE_EDOCR2_PYTHON", None)
os.environ.pop("AIQUOTE_EDOCR2_SOURCE", None)
os.environ.pop("AIQUOTE_EDOCR2_MODEL", None)
os.environ.pop("AIQUOTE_MLSD_TINY_MODEL", None)
if payload.get("converter"):
    os.environ["AIQUOTE_ODA_CONVERTER"] = payload["converter"]
import v3_launcher
original_finder = v3_launcher._OriginalPyzFinder
class LocalBundleFinder(original_finder):
    def __init__(self, archive_path):
        super().__init__(archive_path)
        self.bundle_root = bundle
v3_launcher._OriginalPyzFinder = LocalBundleFinder
if "ezdxf" in sys.modules and not hasattr(sys.modules["ezdxf"], "readfile"):
    for name in list(sys.modules):
        if name == "ezdxf" or name.startswith("ezdxf."):
            del sys.modules[name]
core = v3_launcher.load_v3_namespace()
tools = core["DrawingRecognitionTools"]
renderer = pathlib.Path(payload["renderer"])
if renderer.is_file():
    tools._find_pdftoppm = staticmethod(lambda: str(renderer))
from scheme2_ui import _Scheme2PageRecognitionWorker
from recognition_dimensions import dimension_review_hint
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
import onnxruntime as ort
original_session_init = ort.InferenceSession.__init__
def bounded_session(session, model, sess_options=None, *args, **kwargs):
    options = sess_options if sess_options is not None else ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    original_session_init(session, model, options, *args, **kwargs)
ort.InferenceSession.__init__ = bounded_session
source = pathlib.Path(payload["path"])
page_sources = [(source, 0)]
if source.suffix.lower() == ".pdf":
    from pypdf import PdfReader
    with PdfReader(str(source)) as reader:
        page_sources = [(source, index) for index in range(len(reader.pages))]
elif source.suffix.lower() in (".tif", ".tiff"):
    from PIL import Image
    with Image.open(source) as image:
        page_sources = []
        for index in range(image.n_frames):
            image.seek(index)
            target = source.parent / ("frame-%s.png" % (index+1))
            image.convert("RGB").save(target)
            page_sources.append((target, 0))
pages, candidates, errors = [], [], []
for index, (page_source, source_index) in enumerate(page_sources):
    if index != payload.get("page_index", 0):
        continue
    entry = {"source_path":str(page_source), "page_index":source_index,
             "page_count":len(page_sources), "key":"page-%s" % (index+1)}
    results, failures = [], []
    task = _Scheme2PageRecognitionWorker(tools, entry)
    task.succeeded.connect(lambda _key, value: results.append(value))
    task.failed.connect(lambda _key, message: failures.append(message))
    task.run()
    if failures or not results:
        errors.append({"page":index+1, "message":failures[0] if failures else "未返回识别结果"})
        continue
    result = results[0]
    result["source_page_number"] = index+1
    pages.append(result)
    records = result.get("cabinet_candidates") or core["build_review_candidates"](result) or [result]
    for record in records:
        if not isinstance(record, dict):
            continue
        record["source_page_number"] = index+1
        record["manual_reviewed"] = False
        record["manual_confirmation_checked"] = False
        features = record.get("recognized_features") or {}
        facts = features.get("facts") or {}
        suggested = {}
        dimensions = record.get("dimensions") or []
        recognition = record.get("dimension_recognition") or {}
        if dimensions and recognition.get("status") == "recognized":
            width, height, depth = dimensions[0]
            suggested.update(width_mm=width, height_mm=height, depth_mm=depth)
        # Preserve a detected ganged width instead of flattening it to one total.
        specification = str(record.get("specification") or "")
        split = re.match(r"^\s*[（(]\s*(\d+(?:\.\d+)?(?:\s*\+\s*\d+(?:\.\d+)?)+)\s*[)）]\s*[×xX*/]", specification)
        if split:
            parts = [float(part.strip()) for part in split.group(1).split("+")]
            if 2 <= len(parts) <= 20 and all(part > 0 for part in parts):
                suggested.update(width_expression="+".join(f"{part:g}" for part in parts), width_mm=sum(parts))
        for target, fact_name in (("material","material"), ("thickness_mm","body_thickness_mm")):
            fact = facts.get(fact_name) or {}
            if fact.get("value") is not None and fact.get("origin") == "drawing":
                if target != "material" or fact["value"] in ("SUS304", "SUS316", "SECC", "SGCC"):
                    suggested[target] = fact["value"]
        door = (facts.get("door_layout") or {}).get("value")
        if door:
            suggested["door_type"] = {"front_single":"前单开门", "front_double":"前双开门"}.get(door, str(door))
        code = record.get("product_code")
        if code:
            suggested["product_code"] = str(code)
        text = str(record.get("raw_text") or record.get("text") or "")
        attachments = list(core["MainWindow"].recommend_attachment_names(text) or [])
        notes = []
        if record.get("specification"):
            notes.append("规格候选：" + str(record["specification"]))
        material = facts.get("material") or {}
        if material.get("value"):
            notes.append("材质依据：" + str(material.get("evidence") or material["value"]))
        if record.get("height_terms_mm"):
            notes.append("高度含箱体与底座分项，请按原图核对：" + str(record["height_terms_mm"]))
        if recognition.get("status") != "recognized":
            notes.append(dimension_review_hint(record))
        if attachments:
            notes.append("附件建议（数量待核对）："+"、".join(attachments))
        warnings = record.get("specification_warnings") or []
        notes.extend(str(warning) for warning in warnings)
        summary = "\n".join(dict.fromkeys(note for note in notes if note))
        candidates.append({"name":record.get("name") or result.get("name") or "待复核项目",
                           "page":index+1, "suggested":suggested,
                           "summary":summary or "请对照原图核对选型参数。",
                           "attachment_names":attachments, "raw":record})
from hashlib import sha256
output = {"schema_version":1, "page_count":len(page_sources), "pages":pages,
          "candidates":candidates, "errors":errors,
          "core_manifest_sha256":sha256((client/"v3_core/manifest.json").read_bytes()).hexdigest()}
print("ODOO_RECOGNITION_RESULT="+json.dumps(output,ensure_ascii=False,default=str),flush=True)


'''

_DRAWING_DIMENSION_WORKER = _DRAWING_RECOGNITION_WORKER.split("source = pathlib.Path")[0] + r'''


import re, subprocess, typing
import cv2
import numpy as np
import PIL._typing
# The shipped OCR renderer imports Pillow type aliases newer than Odoo's Pillow.
# These aliases are only annotations; image decoding below stays inside OpenCV.
if not hasattr(PIL._typing, "Coords"):
    PIL._typing.Coords = typing.Sequence[float]
if not hasattr(PIL._typing, "StrOrBytesPath"):
    PIL._typing.StrOrBytesPath = typing.Union[str, bytes, os.PathLike]
source = pathlib.Path(payload["path"])
page_number = payload["page"]
rotation = payload["rotation"]
orientation=payload.get("orientation",0)
display_rotation=rotation
rotation=(rotation-orientation)%360
click_point = list(payload["point"])
u,v = click_point
if rotation == 90: payload["point"] = [v,1-u]
elif rotation == 180: payload["point"] = [1-u,1-v]
elif rotation == 270: payload["point"] = [1-v,u]
manual_rect=payload.get("rect")
if manual_rect:
    a,b,c,d=manual_rect
    if rotation==90:manual_rect=[b,1-a-c,d,c]
    elif rotation==180:manual_rect=[1-a-c,1-b-d,c,d]
    elif rotation==270:manual_rect=[1-b-d,a,d,c]
pdf_page = None
if source.suffix.lower() == ".pdf":
    from pypdf import PdfReader
    reader = PdfReader(str(source))
    pdf_page = reader.pages[page_number-1]
    image_path = source.parent / "dimension-page"
    rendered = subprocess.run([payload["renderer"], "-f", str(page_number), "-l", str(page_number),
        "-singlefile", "-cropbox", "-scale-to", "2400", "-png", str(source), str(image_path)],
        capture_output=True, timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if rendered.returncode:
        raise ValueError("无法渲染当前 PDF 页")
    content = image_path.with_suffix(".png").read_bytes()
else:
    content = source.read_bytes()
image = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
if image is None:
    raise ValueError("无法读取当前图纸")
if orientation:
    image=cv2.rotate(image,{90:cv2.ROTATE_90_CLOCKWISE,180:cv2.ROTATE_180,270:cv2.ROTATE_90_COUNTERCLOCKWISE}[orientation])
height, width = image.shape[:2]
if max(width, height) > 2400:
    ratio = 2400 / max(width, height)
    image = cv2.resize(image, (round(width*ratio), round(height*ratio)))
height, width = image.shape[:2]
px, py = payload["point"][0]*width, payload["point"][1]*height
gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
ink = cv2.bitwise_or(ink,cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY_INV,31,9))
closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3,3),np.uint8))
contours, hierarchy = cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
rectangles = []
cores = []
for contour in contours:
    x,y,w,h = cv2.boundingRect(contour)
    if w < max(20,width*.025) or h < max(25,height*.035) or w*h > width*height*.55:
        continue
    if x <= 2 and x+w >= width-2 or y <= 2 and y+h >= height-2:
        continue
    approximation=cv2.approxPolyDP(contour,cv2.arcLength(contour,True)*.01,True)
    if cv2.contourArea(contour)/(w*h) < .90 or len(approximation)>8 or not cv2.isContourConvex(approximation):
        continue
    if any(abs(x-a[0])+abs(y-a[1])+abs(w-a[2])+abs(h-a[3]) < 15 for a in cores):
        continue
    cores.append((x,y,w,h))
    if not (x-3 <= px <= x+w+3 and y-3 <= py <= y+h+3):
        continue
    rectangles.append((x,y,w,h))
if manual_rect:
    mx,my,mw,mh=manual_rect
    left=max(0,round(mx*width));top=max(0,round(my*height))
    right=min(width,round((mx+mw)*width));bottom=min(height,round((my+mh)*height))
    # Contours help associate labels with cabinet edges, but never change the
    # user-drawn selection or read labels outside it.
    cores=[r for r in cores if r[0]>=left and r[1]>=top and r[0]+r[2]<=right and r[1]+r[3]<=bottom]
    rectangles=[r for r in rectangles if r[0]>=left and r[1]>=top and r[0]+r[2]<=right and r[1]+r[3]<=bottom]
    if not rectangles:
        # Recover an open contour from four connected straight cabinet edges.
        vm=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((max(20,round(height*.035)),1),np.uint8))
        hm=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,max(20,round(width*.025))),np.uint8))
        vertical=[];horizontal=[]
        for c in cv2.findContours(vm,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0]:
            a,b,cw,ch=cv2.boundingRect(c)
            if b-3<=py<=b+ch+3:vertical.append((a+cw/2,b,b+ch))
        for c in cv2.findContours(hm,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0]:
            a,b,cw,ch=cv2.boundingRect(c);horizontal.append((b+ch/2,a,a+cw))
        le=sorted([v for v in vertical if v[0]<=px],key=lambda v:px-v[0])[:12]
        ri=sorted([v for v in vertical if v[0]>=px],key=lambda v:v[0]-px)[:12]
        for l in le:
            for r in ri:
                a,b=l[0],max(l[1],r[1]);cw,ch=r[0]-a,min(l[2],r[2])-b
                if cw<max(20,width*.025) or ch<max(25,height*.035) or cw*ch>width*height*.55:continue
                tolerance=max(5,ch*.02)
                tops=[p for p,u,v in horizontal if b-tolerance<=p<=py and u<=a+5 and v>=a+cw-5]
                bottoms=[p for p,u,v in horizontal if py<=p<=b+ch+tolerance and u<=a+5 and v>=a+cw-5]
                if tops and bottoms:
                    y0,y1=max(tops),min(bottoms)
                    if y1-y0>=max(25,height*.035):rectangles.append(tuple(round(v) for v in (a,y0,cw,y1-y0)))
        if rectangles:rectangles=[min(rectangles,key=lambda r:r[2]*r[3])]
    rectangles=[r for r in rectangles if r[0]>=left and r[1]>=top and r[0]+r[2]<=right and r[1]+r[3]<=bottom]
    if cores or rectangles:
        x,y,w,h=max(rectangles or cores,key=lambda r:r[2]*r[3])
        for _ in range(8):
            before=(x,y,w,h)
            for a,b,c,d in cores:
                if abs(b-y)<max(5,h*.035) and abs(b+d-y-h)<max(5,h*.035) and a<=x+w+max(6,w*.06) and a+c>=x-max(6,w*.06):
                    nx,ny=min(x,a),min(y,b)
                    w,h=max(x+w,a+c)-nx,max(y+h,b+d)-ny;x,y=nx,ny
            if before==(x,y,w,h):break
        # A handle cut-out can open one door's contour. Match the continuous paired
        # cabinet edges beside the clicked interior to include both doors in that case.
        for horizontal in (True,):
            span=w if horizontal else h
            mask=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,max(15,round(span*.6))) if horizontal else (max(15,round(span*.6)),1),np.uint8))
            edges=[]
            for c in cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0]:
                a,b,cw,ch=cv2.boundingRect(c)
                pos,start,end=(b+ch/2,a,a+cw) if horizontal else (a+cw/2,b,b+ch)
                origin=x if horizontal else y
                if start<=origin+span*.1 and end>=origin+span*.9:edges.append((pos,start,end))
            low,high=(y,y+h) if horizontal else (x,x+w)
            tolerance=max(5,(h if horizontal else w)*.035)
            first=sorted([e for e in edges if abs(e[0]-low)<=tolerance],key=lambda e:abs(e[0]-low))
            last=sorted([e for e in edges if abs(e[0]-high)<=tolerance],key=lambda e:abs(e[0]-high))
            if first and last:
                begin=max(first[0][1],last[0][1]);end=min(first[0][2],last[0][2])
                if span<=end-begin<=span*4:
                    if horizontal:x,w=round(begin),round(end-begin)
                    else:y,h=round(begin),round(end-begin)
        # Snap the joined interiors to nearby cabinet edges, excluding the more distant
        # dimension-line rectangle which used to enlarge the entire selected view.
        def nearby_edges(horizontal):
            length=max(15,round((w if horizontal else h)*.65))
            mask=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,length) if horizontal else (length,1),np.uint8))
            found=[]
            for c in cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0]:
                a,b,cw,ch=cv2.boundingRect(c)
                if horizontal and cw>=w*.65 and a<=x+w*.15 and a+cw>=x+w*.85:found.append(b+ch/2)
                elif not horizontal and ch>=h*.65 and b<=y+h*.15 and b+ch>=y+h*.85:found.append(a+cw/2)
            return found
        horizontal_edges,vertical_edges=nearby_edges(True),nearby_edges(False)
        oldx,oldy,oldright,oldbottom=x,y,x+w,y+h
        frame_band=max(8,min(w,h)*.07)
        xs=[v for v in vertical_edges if oldx-frame_band<=v<=oldx+2]
        xe=[v for v in vertical_edges if oldright-2<=v<=oldright+frame_band]
        ys=[v for v in horizontal_edges if oldy-frame_band<=v<=oldy+2]
        ye=[v for v in horizontal_edges if oldbottom-2<=v<=oldbottom+frame_band]
        x=round(min(xs,default=oldx));y=round(min(ys,default=oldy))
        w=round(max(xe,default=oldright))-x;h=round(max(ye,default=oldbottom))-y
        x=max(left,x);y=max(top,y)
        w=min(right-x,w);h=min(bottom-y,h)
        body=(x,y,w,h)
    else:
        body=(left,top,right-left,bottom-top)
    x,y,w,h=body
else:
    if not rectangles:
        # Recover an open contour from four connected straight cabinet edges.
        vm=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((max(20,round(height*.035)),1),np.uint8))
        hm=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,max(20,round(width*.025))),np.uint8))
        vertical=[];horizontal=[]
        for c in cv2.findContours(vm,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0]:
            a,b,cw,ch=cv2.boundingRect(c)
            if b-3<=py<=b+ch+3:vertical.append((a+cw/2,b,b+ch))
        for c in cv2.findContours(hm,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0]:
            a,b,cw,ch=cv2.boundingRect(c);horizontal.append((b+ch/2,a,a+cw))
        le=sorted([v for v in vertical if v[0]<=px],key=lambda v:px-v[0])[:12]
        ri=sorted([v for v in vertical if v[0]>=px],key=lambda v:v[0]-px)[:12]
        for l in le:
            for r in ri:
                a,b=l[0],max(l[1],r[1]);cw,ch=r[0]-a,min(l[2],r[2])-b
                if cw<max(20,width*.025) or ch<max(25,height*.035) or cw*ch>width*height*.55:continue
                tolerance=max(5,ch*.02)
                tops=[p for p,u,v in horizontal if b-tolerance<=p<=py and u<=a+5 and v>=a+cw-5]
                bottoms=[p for p,u,v in horizontal if py<=p<=b+ch+tolerance and u<=a+5 and v>=a+cw-5]
                if tops and bottoms:
                    y0,y1=max(tops),min(bottoms)
                    if y1-y0>=max(25,height*.035):rectangles.append(tuple(round(v) for v in (a,y0,cw,y1-y0)))
        if rectangles:rectangles=[min(rectangles,key=lambda r:r[2]*r[3])]
    if not rectangles:
        # Seed from a nearby closed door when a handle/leader opens the clicked door.
        # The final cabinet bounds below must still contain the actual click.
        nearby=[]
        for a,b,c,d in cores:
            dx=max(a-px,0,px-a-c);dy=max(b-py,0,py-b-d)
            if dx<=c and dy<=max(5,height*.01) or dy<=d and dx<=max(5,width*.01):
                nearby.append((dx*dx+dy*dy,(a,b,c,d)))
        if nearby:rectangles=[min(nearby,key=lambda item:item[0])[1]]
    if not rectangles:
        print("ODOO_DIMENSION_RESULT="+json.dumps({"error":"此处未找到明确的视图轮廓，请点击柜体内部；也可手工填写尺寸。"},ensure_ascii=False))
        sys.exit(0)
    smallest = min(r[2]*r[3] for r in rectangles)
    rectangles = sorted([r for r in rectangles if r[2]*r[3] <= smallest*4],key=lambda r:r[2]*r[3],reverse=True)[:4]
    x,y,w,h = rectangles[0]
    # Join adjacent door interiors belonging to the same view, never distant views.
    for _ in range(8):
        before=(x,y,w,h)
        for a,b,c,d in cores:
            aligned=abs(b-y)<max(5,h*.035) and abs(b+d-y-h)<max(5,h*.035)
            adjacent=a<=x+w+max(6,w*.06) and a+c>=x-max(6,w*.06)
            if aligned and adjacent:
                nx,ny=min(x,a),min(y,b)
                w,h=max(x+w,a+c)-nx,max(y+h,b+d)-ny
                x,y=nx,ny
        if before==(x,y,w,h):break
    # A handle cut-out can open one door's contour. Match the continuous paired
    # cabinet edges beside the clicked interior to include both doors in that case.
    for horizontal in (True,):
        span=w if horizontal else h
        mask=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,max(15,round(span*.6))) if horizontal else (max(15,round(span*.6)),1),np.uint8))
        edges=[]
        for c in cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0]:
            a,b,cw,ch=cv2.boundingRect(c)
            pos,start,end=(b+ch/2,a,a+cw) if horizontal else (a+cw/2,b,b+ch)
            origin=x if horizontal else y
            if start<=origin+span*.1 and end>=origin+span*.9:edges.append((pos,start,end))
        low,high=(y,y+h) if horizontal else (x,x+w)
        tolerance=max(5,(h if horizontal else w)*.035)
        first=sorted([e for e in edges if abs(e[0]-low)<=tolerance],key=lambda e:abs(e[0]-low))
        last=sorted([e for e in edges if abs(e[0]-high)<=tolerance],key=lambda e:abs(e[0]-high))
        if first and last:
            begin=max(first[0][1],last[0][1]);end=min(first[0][2],last[0][2])
            if span<=end-begin<=span*4:
                if horizontal:x,w=round(begin),round(end-begin)
                else:y,h=round(begin),round(end-begin)
    # Snap the joined interiors to nearby cabinet edges, excluding the more distant
    # dimension-line rectangle which used to enlarge the entire selected view.
    def nearby_edges(horizontal):
        length=max(15,round((w if horizontal else h)*.65))
        mask=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,length) if horizontal else (length,1),np.uint8))
        found=[]
        for c in cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0]:
            a,b,cw,ch=cv2.boundingRect(c)
            if horizontal and cw>=w*.65 and a<=x+w*.15 and a+cw>=x+w*.85:found.append(b+ch/2)
            elif not horizontal and ch>=h*.65 and b<=y+h*.15 and b+ch>=y+h*.85:found.append(a+cw/2)
        return found
    horizontal_edges,vertical_edges=nearby_edges(True),nearby_edges(False)
    oldx,oldy,oldright,oldbottom=x,y,x+w,y+h
    frame_band=max(8,min(w,h)*.07)
    xs=[v for v in vertical_edges if oldx-frame_band<=v<=oldx+2]
    xe=[v for v in vertical_edges if oldright-2<=v<=oldright+frame_band]
    ys=[v for v in horizontal_edges if oldy-frame_band<=v<=oldy+2]
    ye=[v for v in horizontal_edges if oldbottom-2<=v<=oldbottom+frame_band]
    x=round(min(xs,default=oldx));y=round(min(ys,default=oldy))
    w=round(max(xe,default=oldright))-x;h=round(max(ye,default=oldbottom))-y
    body=(x,y,w,h)
    if not (x-3<=px<=x+w+3 and y-3<=py<=y+h+3):
        print("ODOO_DIMENSION_RESULT="+json.dumps({"error":"此处未找到明确的视图轮廓，请点击柜体内部；也可手工填写尺寸。"},ensure_ascii=False))
        sys.exit(0)

def distance_to_rect(cx,cy,r):
    a,b,c,d=r
    return max(a-cx,0,cx-a-c)**2+max(b-cy,0,cy-b-d)**2
neighbours=[r for r in cores if r[0]+r[2]<x-5 or r[0]>x+w+5 or r[1]+r[3]<y-5 or r[1]>y+h+5]
# The surrounding band includes dimension text, not the drawing's title block.
if not manual_rect:
    left=max(0,round(x-w*.65)); right=min(width,round(x+w*1.65))
    top=max(0,round(y-h*.25)); bottom=min(height,round(y+h*1.28))
tokens=[]
def token(text, points, confidence, origin):
    clean=re.sub(r"\s+", "",str(text)).replace("，", ".").strip()
    match=re.fullmatch(r"(?:(总宽|宽度|宽|总深|深度|深|总高|高度|高|[WDHwdh])[:：=]?)?(\d+(?:\.\d{1,3})?)(?:[mM]{2}|毫米)?",clean)
    if not match:
        return
    value=float(match.group(2))
    if not 0 < value <= 100000 or confidence < .75:
        return
    points=np.asarray(points,dtype=float)
    a,b=points.min(axis=0);c,d=points.max(axis=0)
    cx,cy=(a+c)/2,(b+d)/2
    if not(left<=cx<=right and top<=cy<=bottom) or (manual_rect and not (a>=left and b>=top and c<=right and d<=bottom)):
        return
    own_distance=distance_to_rect(cx,cy,body)
    if not manual_rect and own_distance and any(distance_to_rect(cx,cy,r)<own_distance*.45 for r in neighbours):
        return
    prefix=match.group(1) or ""
    axis="vertical" if prefix in ("总高","高度","高","H","h") else "horizontal" if prefix else ""
    if axis=="" and (cx < x+w*.12 or cx > x+w*.88) and y <= cy <= y+h:
        axis="vertical"
    if axis=="" and x-w*.15<=cx<=x+w*1.15 and (cy<y+h*.12 or cy>y+h*.88):
        axis="horizontal"
    if not axis:
        return
    if any(item["value"]==value and item["axis"]==axis and abs(item["cx"]-cx)<15 and abs(item["cy"]-cy)<15 for item in tokens):
        return
    tokens.append({"text":str(text).strip(),"value":value,"axis":axis,"cx":cx,"cy":cy,
        "rect":[float(a/width),float(b/height),float((c-a)/width),float((d-b)/height)],
        "confidence":round(float(confidence),3),"origin":origin,"explicit":bool(prefix)})

if pdf_page is not None:
    box=pdf_page.cropbox; cw,ch=float(box.width),float(box.height)
    turn=(int(pdf_page.get("/Rotate",0))+orientation)%360
    def pdf_point(a,b):
        u,v=(a-float(box.left))/cw,1-(b-float(box.bottom))/ch
        if turn==90:u,v=1-v,u
        elif turn==180:u,v=1-u,1-v
        elif turn==270:u,v=v,1-u
        return u*width,v*height
    def visitor(text, cm, tm, font, size):
        for match in re.finditer(r"\S+",text):
            part=match.group(0); offset=match.start()*size*.52; length=len(part)*size*.52
            def transform(a,b):
                q=a*tm[0]+b*tm[2]+tm[4];r=a*tm[1]+b*tm[3]+tm[5]
                return pdf_point(q*cm[0]+r*cm[2]+cm[4],q*cm[1]+r*cm[3]+cm[5])
            token(part,[transform(offset,-size*.2),transform(offset+length,-size*.2),
                        transform(offset+length,size*.85),transform(offset,size*.85)],1,"pdf_text")
    try: pdf_page.extract_text(visitor_text=visitor)
    except Exception: pass

# Raster/scanned drawings need local character reading only; no cabinet AI or remote service.
if not all(any(t["axis"]==axis for t in tokens) for axis in ("horizontal","vertical")):
    engine,problem=core["_rapidocr_reading_contract"]["_load_rapidocr_engine"]()
    if engine is not None:
        roi=image[top:bottom,left:right]
        for angle in (0,90):
            if angle and any(t["axis"]=="vertical" for t in tokens):break
            raster=roi if not angle else cv2.rotate(roi,cv2.ROTATE_90_CLOCKWISE)
            out=engine(raster,use_cls=True)
            if out.boxes is None:continue
            for coords,text,confidence in zip(out.boxes,out.txts,out.scores):
                pts=np.array(coords,dtype=float)
                if angle:
                    pts=np.column_stack([pts[:,1],roi.shape[0]-pts[:,0]])
                pts+=np.array([left,top]);token(text,pts,confidence,"local_ocr")

def line_groups(horizontal):
    length=max(8,round((w if horizontal else h)*.05))
    mask=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,length) if horizontal else (length,1),np.uint8))
    raw=[]
    for c in cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0]:
        a,b,cw,ch=cv2.boundingRect(c)
        if horizontal and cw>=length:raw.append((b+ch/2,a,a+cw))
        elif not horizontal and ch>=length:raw.append((a+cw/2,b,b+ch))
    groups=[]
    for pos,start,end in sorted(raw):
        def can_join(group):
            if abs(group[0]-pos)>=4:return False
            gap=max(0,start-group[2],group[1]-end)
            span=w if horizontal else h
            if gap<=max(5,span*.08):return True
            if gap>span*.3:return False
            lo,hi=(group[2],start) if start>group[2] else (end,group[1])
            return any(t["axis"]==("horizontal" if horizontal else "vertical")
                and abs((t["cy"] if horizontal else t["cx"])-pos)<max(12,(t["rect"][3]*height if horizontal else t["rect"][2]*width)*1.5)
                and lo-5<=(t["cx"] if horizontal else t["cy"])<=hi+5 for t in tokens)
        found=next((g for g in groups if can_join(g)),None)
        if found:found[1]=min(found[1],start);found[2]=max(found[2],end)
        else:groups.append([pos,start,end])
    return groups
hlines,vlines=line_groups(True),line_groups(False)
def crosses(groups, coordinate, start, end, tolerance):
    return any(abs(p-coordinate)<=tolerance and a<=min(start,end)+tolerance and b>=max(start,end)-tolerance for p,a,b in groups)
for item in tokens:
    horizontal=item["axis"]=="horizontal";cx,cy=item["cx"],item["cy"]
    lines=hlines if horizontal else vlines
    spans=[]
    for pos,start,end in lines:
        if horizontal:
            if not (y-h*.3<=pos<=y+h*1.3) or y+5<pos<y+h-5:continue
            if abs(pos-cy)>max(35,h*.1):continue
            if not start-w*.05<=cx<=end+w*.05:continue
            error=(abs(start-x)+abs(end-x-w))/w
            edge=y if pos<y+h/2 else y+h
            linked=error<.2 and crosses(vlines,start,edge,pos,max(8,w*.04)) and crosses(vlines,end,edge,pos,max(8,w*.04))
        else:
            if not (x-w*.5<=pos<=x+w*1.5) or x+5<pos<x+w-5:continue
            if abs(pos-cx)>max(35,w*.2):continue
            if not start-h*.05<=cy<=end+h*.05:continue
            error=(abs(start-y)+abs(end-y-h))/h
            edge=x if pos<x+w/2 else x+w
            linked=error<.2 and crosses(hlines,start,edge,pos,max(8,h*.04)) and crosses(hlines,end,edge,pos,max(8,h*.04))
        distance=abs(pos-(cy if horizontal else cx))
        spans.append((distance,error,linked))
    nearest=min(spans,key=lambda pair:pair[0]) if spans else None
    item["linked"]=bool(nearest and nearest[2])
    item["score"]=round((8 if item["explicit"] else 0)+(6 if item["linked"] else 0)+(5-12*nearest[1] if nearest else 0),3)
    item["explicit_label"]=item["explicit"]
    item.pop("cx");item.pop("cy");item.pop("explicit")
for axis in ("horizontal","vertical"):
    if any(t["axis"]==axis and t["linked"] for t in tokens):
        tokens=[t for t in tokens if t["axis"]!=axis or t["linked"] or t["explicit_label"]]
tokens=sorted(tokens,key=lambda t:(t["axis"],-t["score"],-t["confidence"]))[:50]
for index,item in enumerate(tokens):item["id"]=str(index+1)
def best(axis):
    group=[t for t in tokens if t["axis"]==axis]
    if not group:return None
    head=group[0];others=[t for t in group if t["value"]!=head["value"]]
    if not head["linked"] and not head["explicit_label"]:return None
    if others and head["score"]-others[0]["score"]<.7:return None
    return head["id"]
def display_rect(r):
    x,y,w,h=r
    if rotation==90:x,y,w,h=1-y-h,x,h,w
    elif rotation==180:x,y=1-x-w,1-y-h
    elif rotation==270:x,y,w,h=y,1-x-w,h,w
    return [round(v,6) for v in (x,y,w,h)]
def norm(r):return display_rect([r[0]/width,r[1]/height,r[2]/width,r[3]/height])
for item in tokens:item["rect"]=display_rect(item["rect"])
body_rect=norm(body)
rx,ry,rw,rh=body_rect
extent=[rx,ry,rx+rw,ry+rh]
for item in tokens:
    a,b,c,d=item["rect"]
    extent=[min(extent[0],a),min(extent[1],b),max(extent[2],a+c),max(extent[3],b+d)]
regions=[[max(0,extent[0]-.004),max(0,extent[1]-.004),min(1,extent[2]+.004)-max(0,extent[0]-.004),min(1,extent[3]+.004)-max(0,extent[1]-.004)],body_rect]
if manual_rect:regions=[display_rect(manual_rect)]
output={"selection_mode":"manual" if manual_rect else "auto","reader_version":3,"page":page_number,"rotation":display_rotation,"orientation":orientation,"regions":regions,"body":body_rect,
    "tokens":tokens,"horizontal":best("horizontal"),"vertical":best("vertical"),"point":click_point}
print("ODOO_DIMENSION_RESULT="+json.dumps(output,ensure_ascii=False),flush=True)



'''

_DRAWING_ORIENTATION_WORKER = r'''
import json,sys
payload=json.load(sys.stdin)


import json, os, pathlib, socket, sys

os.environ.update(QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1",
                  OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
def no_network(*args, **kwargs):
    raise OSError("Local drawing recognition has no network access")
socket.create_connection = no_network
socket.socket.connect = no_network
client = pathlib.Path(payload["client"])
bundle = pathlib.Path(payload["bundle"])
sys.path.insert(0, str(client))
sys.path.extend([payload["dependencies"], str(bundle), payload["pure_dependencies"]])
handles = [os.add_dll_directory(str(path)) for path in
           (bundle, bundle/"onnxruntime/capi") if path.is_dir()]
os.environ["AI_QUOTE_V3_CORE_ROOT"] = str(client/"v3_core")
os.environ.pop("AIQUOTE_EDOCR2_PYTHON", None)
os.environ.pop("AIQUOTE_EDOCR2_SOURCE", None)
os.environ.pop("AIQUOTE_EDOCR2_MODEL", None)
os.environ.pop("AIQUOTE_MLSD_TINY_MODEL", None)
if payload.get("converter"):
    os.environ["AIQUOTE_ODA_CONVERTER"] = payload["converter"]

from drawing_orientation import pdf_view_rotation,raster_view_rotation
source=pathlib.Path(payload["path"])
angle=None;method="local_text"
if source.suffix.lower()==".pdf":
    from pypdf import PdfReader
    with PdfReader(str(source)) as reader:
        angle=pdf_view_rotation(reader.pages[payload["page"]-1])
    if angle is not None:method="pdf_visible_text"
if angle is None:
    # Only scanned/ambiguous pages load the OCR runtime. Most vector PDFs
    # finish using visible text in a fraction of the former cold-start time.
    exec('\n\nimport json, os, pathlib, socket, sys\n\nos.environ.update(QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1",\n                  OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")\ndef no_network(*args, **kwargs):\n    raise OSError("Local drawing recognition has no network access")\nsocket.create_connection = no_network\nsocket.socket.connect = no_network\nclient = pathlib.Path(payload["client"])\nbundle = pathlib.Path(payload["bundle"])\nsys.path.insert(0, str(client))\nsys.path.extend([payload["dependencies"], str(bundle), payload["pure_dependencies"]])\nhandles = [os.add_dll_directory(str(path)) for path in\n           (bundle, bundle/"onnxruntime/capi") if path.is_dir()]\nos.environ["AI_QUOTE_V3_CORE_ROOT"] = str(client/"v3_core")\nos.environ.pop("AIQUOTE_EDOCR2_PYTHON", None)\nos.environ.pop("AIQUOTE_EDOCR2_SOURCE", None)\nos.environ.pop("AIQUOTE_EDOCR2_MODEL", None)\nos.environ.pop("AIQUOTE_MLSD_TINY_MODEL", None)\nif payload.get("converter"):\n    os.environ["AIQUOTE_ODA_CONVERTER"] = payload["converter"]\nimport v3_launcher\noriginal_finder = v3_launcher._OriginalPyzFinder\nclass LocalBundleFinder(original_finder):\n    def __init__(self, archive_path):\n        super().__init__(archive_path)\n        self.bundle_root = bundle\nv3_launcher._OriginalPyzFinder = LocalBundleFinder\nif "ezdxf" in sys.modules and not hasattr(sys.modules["ezdxf"], "readfile"):\n    for name in list(sys.modules):\n        if name == "ezdxf" or name.startswith("ezdxf."):\n            del sys.modules[name]\ncore = v3_launcher.load_v3_namespace()\ntools = core["DrawingRecognitionTools"]\nrenderer = pathlib.Path(payload["renderer"])\nif renderer.is_file():\n    tools._find_pdftoppm = staticmethod(lambda: str(renderer))\nfrom scheme2_ui import _Scheme2PageRecognitionWorker\nfrom recognition_dimensions import dimension_review_hint\nfrom PySide6.QtWidgets import QApplication\napp = QApplication.instance() or QApplication([])\nimport onnxruntime as ort\noriginal_session_init = ort.InferenceSession.__init__\ndef bounded_session(session, model, sess_options=None, *args, **kwargs):\n    options = sess_options if sess_options is not None else ort.SessionOptions()\n    options.intra_op_num_threads = 2\n    options.inter_op_num_threads = 1\n    options.add_session_config_entry("session.intra_op.allow_spinning", "0")\n    options.add_session_config_entry("session.inter_op.allow_spinning", "0")\n    original_session_init(session, model, options, *args, **kwargs)\nort.InferenceSession.__init__ = bounded_session\n')
    import cv2,numpy as np,subprocess,typing
    import PIL._typing
    from PySide6.QtGui import QImage
    if not hasattr(PIL._typing,"Coords"):PIL._typing.Coords=typing.Sequence[float]
    if not hasattr(PIL._typing,"StrOrBytesPath"):PIL._typing.StrOrBytesPath=typing.Union[str,bytes,os.PathLike]
    if source.suffix.lower()==".pdf":
        target=source.parent/"orientation-page"
        result=subprocess.run([payload["renderer"],"-f",str(payload["page"]),"-l",str(payload["page"]),
            "-singlefile","-cropbox","-scale-to","1800","-png",str(source),str(target)],
            capture_output=True,timeout=25,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if result.returncode:raise ValueError("当前页面无法渲染")
        content=target.with_suffix(".png").read_bytes()
    else:content=source.read_bytes()
    pixels=cv2.imdecode(np.frombuffer(content,np.uint8),cv2.IMREAD_COLOR)
    encoded=cv2.imencode(".png",pixels)[1].tobytes() if pixels is not None else b""
    image=QImage.fromData(encoded)
    if not image.isNull():
        engine,problem=core["_rapidocr_reading_contract"]["_load_rapidocr_engine"]()
        if engine is not None:
            def read_text(data):
                pixels=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
                return engine(pixels,use_cls=False) if pixels is not None else None
            angle=raster_view_rotation(image,read_text,lambda:False)
print("ODOO_ORIENTATION_RESULT="+json.dumps({"angle":angle,"confirmed":angle is not None,"method":method}),flush=True)

'''

INVOICE_STATUS = [
    ("upselling", "Upselling Opportunity"),
    ("invoiced", "Fully Invoiced"),
    ("to invoice", "To Invoice"),
    ("no", "Nothing to Invoice"),
]

SALE_ORDER_STATE = [
    ("draft", "Quotation"),
    ("sent", "Quotation Sent"),
    ("sale", "Sales Order"),
    ("cancel", "Cancelled"),
]


class SaleOrder(models.Model):
    _name = "sale.order"
    _explanation = "Represents a customer quotation that can be converted into a sales order. Used"
    " to manage pricing, product quantities, and status"
    _inherit = [
        "account.document.import.mixin",
        "mail.activity.mixin",
        "mail.thread.subject.suggested",
        "portal.mixin",
        "product.catalog.mixin",
        "utm.mixin",
    ]
    _description = "Sales Order"
    _order = "date_order desc, id desc"
    _check_company_auto = True
    _mail_post_access = "read"

    _date_order_conditional_required = models.Constraint(
        "CHECK((state = 'sale' AND date_order IS NOT NULL) OR state != 'sale')",
        "A confirmed sales order requires a confirmation date.",
    )

    @property
    def _rec_names_search(self):
        if self.env.context.get("sale_show_partner_name"):
            return ["name", "partner_id.name"]
        return ["name"]

    # === FIELDS ===#

    drawing_ids = fields.One2many(
        "sale.order.drawing", "order_id", string="图纸与选型", copy=False,
    )
    saved_quote_linked = fields.Boolean(compute="_compute_saved_quote_linked")

    @api.depends("client_order_ref")
    def _compute_saved_quote_linked(self):
        for order in self:
            order.saved_quote_linked = bool(re.fullmatch(r"AIQUOTE:(BJ-\d{8}-\d+)/V([1-9]\d*)", order.client_order_ref or ""))

    def _attach_saved_quote_case(self, case, read_at):
        self.ensure_one()
        self.check_access("write")
        if not self.env.user.has_group("base.group_user"):
            raise AccessError("原报价结果仅供内部读取。")
        expected_ref = f"AIQUOTE:{case['scheme_no']}/V{case['version']}"
        if self.client_order_ref != expected_ref or self.currency_id.name != "CNY":
            raise ValidationError("原报价编号、版本或币种不一致，未读取结果。")
        lines = self.order_line.filtered(lambda line: not line.display_type).sorted(lambda line: (line.sequence, line.id))
        rows, items = case["lines"], case["items"]
        if len(lines) != len(rows) or len(rows) != len(items):
            raise ValidationError("当前报价产品行数与原版本不一致，未读取结果。")
        from decimal import Decimal
        for line, row, item in zip(lines, rows, items, strict=True):
            expected_name = f"{row['name']}\n规格：{row['specification']}"
            # Match the same frozen commercial rows used by the original demo
            # importer. Ordinal identity remains necessary for duplicate names.
            if (line.name != expected_name
                    or Decimal(str(line.product_uom_qty)) != Decimal(str(row["quantity"]))
                    or self.currency_id.compare_amounts(line.price_unit, float(row["quick_unit"]))
                    or self.currency_id.compare_amounts(line.price_subtotal, float(row["quick_total"]))
                    or line.product_uom_id.name != row["unit"]):
                raise ValidationError("当前报价的产品、规格、单位、数量或售价与原版本不一致，原结果已保留。")
            selected = line.selection_values or {}
            for key, source_key in (("width_mm", "width_mm"), ("depth_mm", "depth_mm"),
                                    ("height_mm", "height_mm"), ("thickness_mm", "cabinet_body_thickness_mm")):
                if key in selected and selected[key] and item.get(source_key) is not None:
                    if Decimal(str(selected[key])) != Decimal(str(item[source_key])):
                        raise ValidationError("当前选型尺寸或料厚已变化，不能套用原版本成本。")
            if selected.get("material") and item.get("material_code") and selected["material"] != item["material_code"]:
                raise ValidationError("当前材质已变化，不能套用原版本成本。")
            previous = line.saved_result_snapshot or {}
            if previous and previous.get("signature") != line._saved_result_signature():
                raise ValidationError("当前参数与上次读取时不同，请先核对原报价版本，原结果已保留。")
        for ordinal, (line, item) in enumerate(zip(lines, items, strict=True), 1):
            line._store_saved_result(case, item, ordinal, read_at)
        return len(lines)

    def action_read_saved_quote_results(self):
        self.ensure_one()
        self.check_access("write")
        if not self.env.user.has_group("base.group_user"):
            raise AccessError("原报价结果仅供内部读取。")
        match = re.fullmatch(r"AIQUOTE:(BJ-\d{8}-\d+)/V([1-9]\d*)", self.client_order_ref or "")
        if not match:
            raise UserError("当前报价尚未关联原报价编号和版本，未读取结果。")
        from runpy import run_path
        helper = run_path(r"G:\京能\Odoo\京能板件演示\私有导入\复核正式报价来源.py")
        number, version = match[1], int(match[2])
        selected = None
        try:
            with helper["QuoteClient"]() as client:
                header = next((row for row in client.list_quotes() if row["scheme_no"] == number), None)
                if header is None:
                    raise UserError("原报价记录不可见或已移除，原结果已保留。")
                detail = client.detail(number)
                for found, company, date, items, total, provenance in helper["formal_versions"](header, detail):
                    if found == version:
                        selected = helper["normalize"](number, version, company, date, items, total)
                        selected.update(items=items, provenance=provenance, lock_version=detail.get("lock_version"),
                                        source_path="snapshot/contract_quote_snapshot" if provenance == "formal_snapshot" else f"snapshot/quote_management/versions[version={version}]")
                        break
        except UserError:
            raise
        except Exception:
            raise UserError("原报价结果暂未读取成功，请稍后重试，原结果已保留。") from None
        if selected is None:
            raise UserError("原报价未找到对应已确认版本，未使用当前草稿或其他版本代替。")
        count = self._attach_saved_quote_case(selected, fields.Datetime.to_string(fields.Datetime.now()))
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": "读取完成", "message": f"已读取 {count} 个产品的原报价结果。", "type": "success",
            "next": {"type": "ir.actions.client", "tag": "soft_reload"}}}

    selection_pricing_error = fields.Char(string="最近计价错误", copy=False, groups="base.group_user")

    selection_quote_snapshot = fields.Json(string="选型报价计算快照", copy=False)
    selection_quote_stale = fields.Boolean(compute="_compute_selection_quote_stale")

    @api.depends("selection_quote_snapshot", "selection_price_settings", "order_line", "order_line.selection_values",
                 "order_line.product_id", "order_line.product_uom_qty", "order_line.price_unit",
                 "order_line.discount", "order_line.tax_ids", "drawing_recognition_version",
                 "pricelist_id", "currency_id", "partner_id")
    def _compute_selection_quote_stale(self):
        for order in self:
            snapshot = order.selection_quote_snapshot or {}
            order.selection_quote_stale = bool(snapshot and (
                snapshot.get("signature") != order._selection_quote_signature()))

    def _selection_quote_signature(self):
        self.ensure_one()
        inputs = {
            "partner": self.partner_id.id, "company": self.company_id.id, "price_settings": self.selection_price_settings,
            "currency": self.currency_id.id, "pricelist": self.pricelist_id.id,
            "lines": [{"id": line.id, "product": line.product_id.id,
                       "values": line.selection_values, "quantity": line.product_uom_qty,
                       "price": line.price_unit, "discount": line.discount,
                       "taxes": line.tax_ids.ids, "text": line.name,
                       "type": line.display_type, "sequence": line.sequence}
                      for line in self.order_line.sorted("id")],
            "drawings": [{"id": drawing.id, "token": drawing.recognition_token,
                          "state": drawing.state, "recognition": drawing.recognition_state,
                          "values": {name: drawing[name] for name in drawing._selection_field_names()
                                     if name != "product_template_id"},
                          "candidates": [{"id": c.id, "state": c.review_state,
                                          "excluded": c.excluded_from_quote,
                                          "values": c.review_values or c.suggested_values}
                                         for c in drawing._current_recognition_candidates()]}
                         for drawing in self.drawing_ids.sorted("id")],
        }
        return sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True,
                                 default=str).encode()).hexdigest()

    def _selection_quote_issues(self, rows):
        issues = []
        for row in rows:
            if row.get("display_type"):
                continue
            missing = []
            if not row.get("product_id") or not row.get("product_series"):
                missing.append("产品")
            for key, label in (("width_mm", "宽度"), ("depth_mm", "深度"),
                               ("height_mm", "高度"), ("quantity", "数量"),
                               ("thickness_mm", "料厚")):
                try:
                    value = float(row.get(key) or 0)
                except (TypeError, ValueError):
                    value = 0
                if not math.isfinite(value) or value <= 0:
                    missing.append(label)
            if not row.get("material"):
                missing.append("材质")
            allocations = (row.get("accessory_selection_data") or {}).get("allocations", [])
            if any(item.get("selected", True) and (item.get("scope") == "unassigned" or item.get("quantity") is None)
                   for item in allocations):
                missing.append("附件对象或数量")
            if row.get("source") == "drawing" and row.get("state") != "confirmed":
                missing.append("确认选型")
            if row.get("recognition_state") in ("queued", "processing"):
                missing.append("图纸仍在识别")
            if missing:
                issues.append({"key": row["key"], "missing": missing})
        return issues

    selection_price_settings = fields.Json(string="材料与计价参数", copy=True, groups="base.group_user",
        default=lambda self: self._selection_pricing_defaults())

    def _selection_pricing_defaults(self):
        client = self.env['ir.config_parameter'].sudo().get_str('sale.drawing_recognition.client')
        if not client or not (Path(client) / 'scheme2_ui.py').is_file():
            return {}
        key = (client, (Path(client) / 'scheme2_ui.py').stat().st_mtime_ns)
        if key not in _SELECTION_PRICING_DEFAULT_CACHE:
            _SELECTION_PRICING_DEFAULT_CACHE[key] = self._selection_pricing_worker('defaults', {})
        return json.loads(json.dumps(_SELECTION_PRICING_DEFAULT_CACHE[key]))

    @api.constrains('selection_price_settings')
    def _check_selection_price_settings(self):
        for order in self:
            settings = order.selection_price_settings or {}
            for key in ('galvanized_price', 'carbon_price', 'stainless_304_price', 'stainless_316_price', 'waste_factor'):
                if key in settings and (isinstance(settings[key], bool) or not isinstance(settings[key], (int, float))
                    or not math.isfinite(settings[key]) or settings[key] <= 0):
                    raise ValidationError('材料单价和废料系数必须为有效正数。')

    def _selection_pricing_worker(self, operation, payload):
        client_path = self.env['ir.config_parameter'].sudo().get_str('sale.drawing_recognition.client')
        if not client_path or not (Path(client_path) / 'scheme2_ui.py').is_file():
            raise UserError('原报价规则目录未配置，当前价格未修改。')
        try:
            completed = subprocess.run([sys.executable, '-c', _SELECTION_PRICING_WORKER, client_path],
                input=json.dumps({'operation': operation, **payload}, ensure_ascii=False, allow_nan=False, default=str).encode('utf-8'),
                capture_output=True, timeout=35,
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'},
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if completed.returncode:
                lines = completed.stderr.decode('utf-8', errors='replace').strip().splitlines()
                message = lines[-1] if lines else '原规则处理失败'
                raise UserError(message.removeprefix('ValueError: ')[:350])
            return json.loads(completed.stdout.decode('utf-8'))
        except (subprocess.SubprocessError, OSError, ValueError):
            raise UserError('原报价规则处理未完成，当前价格未修改。') from None

    def _selection_pricing_request(self, client, path, payload=None):
        from urllib.request import Request
        from urllib.error import HTTPError, URLError
        allowed = {'/api/products/catalog', '/api/attachments/catalog?v=2',
                   '/api/attachments/preview', '/api/quotes/calculate-dual', '/api/quotes/calculate-ganged'}
        if path not in allowed:
            raise ValidationError('无效的计价请求。')
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        try:
            with client.opener.open(Request(client.origin + path, data=body, headers=client.headers), timeout=180) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                detail = json.loads(error.read(262144)).get('message', '')
            except (ValueError, OSError):
                detail = ''
            if detail and re.search('[\u4e00-\u9fff]', detail) and not re.search('postgres|password|connection|SELECT |INSERT |UPDATE ', detail, re.I):
                raise UserError(f'原计价服务：{detail[:280]}。当前订单金额未修改。') from None
            raise UserError(f'原计价服务返回 HTTP {error.code}，当前订单金额未修改。') from None
        except (URLError, TimeoutError, ValueError, OSError):
            raise UserError('原计价服务连接未完成。当前金额已保留，请勿连续点击；稍后重新计算。') from None

    def _selection_pricing_plan(self, row, products, catalog):
        line = self.env['sale.order.line'].browse(row['line_id'])
        values = dict(line.selection_values or {})
        settings = dict(self.selection_price_settings or self._selection_pricing_defaults())
        series = row['product_series']
        if self.currency_id.name != 'CNY':
            raise UserError('原报价服务以人民币计价，请先使用人民币价目表。')
        quantity = float(row['quantity'])
        if quantity != int(quantity) or quantity <= 0:
            raise ValidationError('原报价规则要求订单数量为正整数。')
        material = str(row['material']).strip().upper()
        if material not in ('SECC', 'SUS304', 'SUS316'):
            raise ValidationError('请选择 SECC、SUS304 或 SUS316 材质后重新计算。')
        cabinets = row['cabinet_configuration']['cabinets']
        children = []
        available = {entry['product_code'] for entry in products['items']}
        for cabinet in cabinets:
            single, double = cabinet['single_door_count'], cabinet['double_door_count']
            variant = 'SINGLE' if single else 'DOUBLE'
            code = ({'JC': 'JC_EXP', 'JQ': 'JQ_EXP', 'JP WIDE': 'JP_WIDE_EXP',
                     'JS WIDE': 'JS_WIDE_EXP', '操作台': 'OP_TABLE_EXP'}.get(series)
                    or ('JA_SINGLE' if series == 'JA' else f'{series}_{variant}' if series in ('JE', 'JP', 'JS') else series))
            if code not in available:
                raise ValidationError(f'原价格目录中没有当前产品及门型：{series}。')
            profile = values.get('variant_code') or ('WIDE' if 'WIDE' in series else variant)
            model = str(values.get('model_code') or '')
            if series == 'JC' and profile not in ('LUXURY', 'STANDARD') and not re.search(r'-[12]$', model):
                raise ValidationError('JC 需要明确豪华型(-1)或标配型(-2)型号，不能自动选择其中一个价格。')
            child = {'quote_id': 'ODOO_' + uuid.uuid4().hex, 'product_code': code, 'variant_code': profile,
                'model_code': model, 'material_code': material, 'width_mm': cabinet['width_mm'],
                'height_mm': row['height_mm'], 'depth_mm': row['depth_mm'],
                'single_door_count': single, 'double_door_count': double, 'door_position': cabinet['door_position'],
                'cabinet_body_thickness_mm': row['thickness_mm'], 'waste_factor': float(settings.get('waste_factor', 1.2)),
                'coating_type': {'orange': '橘纹', 'gloss': '平光', 'none': '不喷塑'}.get(row.get('coating_type'), row.get('coating_type') or '橘纹'),
                'quote_date': fields.Date.to_string(fields.Date.context_today(self)),
                'pricing_basis': 'per_unit_set_v1', 'attachments': []}
            material_key = {'SECC': 'carbon_price', 'SUS304': 'stainless_304_price', 'SUS316': 'stainless_316_price'}[material]
            child.update(galvanized_sheet_unit_price_override=settings.get('galvanized_price'),
                carbon_steel_unit_price_override=settings.get('carbon_price'),
                material_unit_price_override=settings.get(material_key),
                surface_treatment_unit_price_override=(settings.get('surface_prices') or {}).get(row.get('coating_type'), settings.get('surface_price')))
            for key in ('galvanized_sheet_unit_price_override', 'carbon_steel_unit_price_override',
                        'material_unit_price_override', 'surface_treatment_unit_price_override', 'base_height_mm'):
                if values.get(key) is not None:
                    child[key] = values[key]
            children.append(child)
        if not children:
            raise ValidationError('请先填写有效的柜体宽度。')
        environment = {**children[0], 'width_mm': row['width_mm'], 'attachment_contract': 2,
                       'attachment_material_price_contract': 1, 'ganged_cabinet_count': len(children)}
        dimensions = [{key: child[key] for key in ('product_code', 'material_code', 'width_mm', 'height_mm',
                      'depth_mm', 'single_door_count', 'double_door_count', 'door_position', 'cabinet_body_thickness_mm')}
                      for child in children]
        if len(children) > 1:
            environment.update(ganged_cabinets=dimensions, ganged_cabinet_inputs=dimensions)
        allocations = (row.get('accessory_selection_data') or {}).get('allocations', [])
        if any(a.get('selected', True) and a.get('scope') not in ('cabinet', 'group') for a in allocations):
            raise ValidationError('请先为所选附件明确柜位或整组归属。')
        matched = self._selection_pricing_worker('match', {'catalog': catalog, 'allocations': allocations,
            'cabinets': children, 'environment': environment})
        if len(matched['selected']) > 100:
            raise ValidationError('一行最多支持100项附件，请合并重复项目。')
        environment['attachments'] = matched['selected']
        item = {**environment, 'name': line.quotation_name or row['product_name'],
                'specification': line.quotation_specification or '', 'quantity': int(quantity),
                'quick_discount': line.selection_discount_coefficient,
                'freight_mode': values.get('freight_mode', 'AUTO'),
                'freight_fee': float(values.get('freight_fee', 0)),
                'quick_freight_basis': 'separate_unit_freight_v1',
                'quick_freight_mode': values.get('quick_freight_mode', 'FOLLOW_COST'),
                'scheme2_cost_settings': settings}
        if item['quick_freight_mode'] == 'MANUAL':
            item['quick_freight_fee'] = values.get('quick_freight_fee')
        endpoint = '/api/quotes/calculate-dual'
        payload = environment
        if len(children) > 1:
            endpoint = '/api/quotes/calculate-ganged'
            payload = {'cabinets': children, 'attachment_total': 0, 'attachment_payload': environment}
        return {'line_id': line.id, 'endpoint': endpoint, 'payload': payload,
                'environment': environment, 'matched': matched['matched'], 'item': item,
                'catalog_version': catalog['data_version'], 'matcher_sha256': matched['source_sha256']}

    def _calculate_selection_prices(self, rows):
        self.ensure_one()
        if not self.env.user.has_group('base.group_user'):
            raise AccessError('计价仅供内部用户使用。')
        from runpy import run_path
        helper = run_path(r'G:\京能\Odoo\京能板件演示\私有导入\复核正式报价来源.py')
        try:
            with helper['QuoteClient']() as client:
                products = self._selection_pricing_request(client, '/api/products/catalog')
                catalog = self._selection_pricing_request(client, '/api/attachments/catalog?v=2')
                if catalog.get('status') != 'ACTIVE' or catalog.get('attachment_contract') != 2:
                    raise UserError('原报价附件目录尚未启用，当前金额已保留。')
                plans = [self._selection_pricing_plan(row, products, catalog) for row in rows]
                # Validate every attachment before any persistent calculation request.
                for plan in plans:
                    if not plan['environment']['attachments']:
                        continue
                    preview = self._selection_pricing_request(client, '/api/attachments/preview', plan['environment'])
                    self._selection_validate_pricing_response(preview, plan, attachments_only=True)
                results = {}
                for plan in plans:
                    response = self._selection_pricing_request(client, plan['endpoint'], plan['payload'])
                    resolved_risks = self._selection_validate_pricing_response(response, plan)
                    finished = self._selection_pricing_worker('finish', {'item': plan['item'], 'response': response, 'matched': plan['matched']})
                    finished.update(response=response, request=plan['payload'], catalog_version=plan['catalog_version'],
                                    matcher_sha256=plan['matcher_sha256'], account=helper['ACCOUNT'], resolved_risks=resolved_risks)
                    results[plan['line_id']] = finished
                return results
        except (UserError, ValidationError, AccessError):
            raise
        except Exception:
            _logger.exception('Original quotation calculation did not complete')
            raise UserError('原报价计算未完成，当前订单价格和历史结果已保留。') from None

    def _selection_validate_pricing_response(self, response, plan, attachments_only=False):
        attachments = response.get('attachments') or []
        if len(attachments) != len(plan['matched']):
            raise ValidationError('原服务返回的附件数量不一致，当前金额未修改。')
        for attachment in attachments:
            if attachment.get('status') in ('ERROR', 'PENDING_MANUAL') or attachment.get('pending_manual_dimensions'):
                line = self.env['sale.order.line'].browse(plan['line_id'])
                values = json.loads(json.dumps(line.selection_values or {}))
                for allocation in (values.get('accessory_selection_data') or {}).get('allocations', []):
                    if allocation.get('name') == attachment.get('item_name') and (len(plan['item'].get('ganged_cabinets') or []) <= 1 or allocation.get('cabinet_index') == attachment.get('ganged_cabinet_index')):
                        allocation['manual_parameters'] = [p for p in attachment.get('required_parameters', []) if p.get('source') == 'MANUAL']
                line.selection_values = values
                raise ValidationError('附件需要补充实际尺寸：' + str(attachment.get('item_name') or '') + '。请在配置内填写后重新计算，当前金额已保留。')
            if attachment.get('catalog_version') != plan['catalog_version']:
                raise ValidationError('附件目录版本已变化，请重新匹配，当前金额未修改。')
            for key in ('quick_amount', 'formula_amount'):
                if key == 'formula_amount' and attachment.get('status') == 'QUICK_ONLY':
                    continue  # Explicit original rule: sale-only attachments have no formula cost.
                value = attachment.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValidationError('附件计算结果不完整，当前金额未修改。')
        if attachments_only:
            return
        for section, keys in (('formula_cost', ('material_cost', 'auxiliary_cost', 'labor_cost', 'spray_cost', 'total_cost')),
                              ('quick_quote', ('base_price', 'total_cost'))):
            for key in keys:
                value = (response.get(section) or {}).get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValidationError('原价格库未命中完整计价规则，当前金额未修改。')
        if not response.get('quote_line_id'):
            raise ValidationError('原服务没有返回完整计算快照，当前金额未修改。')
        resolved = []
        children = response.get('ganged_cabinet_results') or [response]
        expected = plan['payload'].get('cabinets') or [plan['payload']]
        summaries = (response.get('formula_cost') or {}).get('ganged_cabinet_costs') or []
        context_matches = len(children) == len(expected) and response.get('quote_id') == expected[0].get('quote_id')
        if len(expected) > 1:
            context_matches = context_matches and len(summaries) == len(expected)
            for summary, submitted, child in zip(summaries, expected, children):
                context_matches = context_matches and child.get('quote_id') == submitted.get('quote_id') and all(
                    str(summary.get(key)) == str(submitted.get(key)) for key in ('product_code',)) and all(
                    float(summary.get(key, -1)) == float(submitted[key]) for key in ('width_mm', 'height_mm', 'depth_mm'))
        def verified_material(child, submitted):
            from decimal import Decimal, ROUND_HALF_UP
            formula = child.get('formula_cost') or {}
            parts = formula.get('cabinet_material_part_details') or []
            if formula.get('cabinet_material_method') != 'AREA_FORMULA' or not formula.get('cabinet_material_version') or not parts:
                return False
            total, weight = Decimal(0), Decimal(0)
            for part in parts:
                material = part.get('material_code')
                if material != (part.get('fixed_material_code') or submitted['material_code']):
                    return False
                key = 'galvanized_sheet_unit_price_override' if material == 'SGCC' else 'carbon_steel_unit_price_override' if material == 'SECC' else 'material_unit_price_override'
                numbers = [part.get(key) for key in ('material_unit_price','billable_weight_kg','material_cost','area_m2','internal_quantity','density_g_cm3','net_weight_kg','sheet_thickness_mm','waste_factor')] + [submitted.get(key)]
                if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in numbers):
                    return False
                price, billable, cost, area, quantity, density, net, thickness, waste, selected_price = numbers
                if not part.get('rule_id') or not part.get('source_sheet') or min(area,quantity,billable,cost,net) < 0 or min(price,density,thickness,waste) <= 0:
                    return False
                if abs(price-selected_price)>1e-8 or abs(waste-submitted['waste_factor'])>1e-8 or abs(cost-price*billable)>1e-5 or abs(net-area*thickness*quantity*density)>1e-5 or abs(billable-net*waste)>1e-5:
                    return False
                total += Decimal(str(cost)); weight += Decimal(str(billable))
            return total.quantize(Decimal('.01'),rounding=ROUND_HALF_UP) == Decimal(str(formula['material_cost'])).quantize(Decimal('.01')) and abs(float(weight)-formula.get('corrected_material_weight_kg',-1))<1e-7
        material_replaced = context_matches and all(verified_material(child, submitted) for child,submitted in zip(children,expected))
        original_flags = [(index,flag) for index,child in enumerate(children) for flag in child.get('risk_flags',[])]
        for risk_index,flag in enumerate(response.get('risk_flags',[])):
            if not (flag.get('severity') in ('blocker','error') or flag.get('blocking')):
                continue
            if flag.get('code') == 'formula_material_cost_missing' and material_replaced and risk_index < len(original_flags) and original_flags[risk_index][1] == flag:
                child_index=original_flags[risk_index][0]
                resolved.append({'code':flag['code'],'reason':'verified_versioned_cabinet_material_replaces_legacy',
                    'risk_index':risk_index,'child_index':child_index,'quote_id':children[child_index]['quote_id'],
                    'version':children[child_index]['formula_cost']['cabinet_material_version'],
                    'checked_at':fields.Datetime.to_string(fields.Datetime.now()),
                    'basis':'submitted_context_and_part_prices_density_area_quantity_weight_cost_verified'})
            else:
                raise ValidationError('原服务计价问题：'+str(flag.get('message') or flag.get('code') or '缺少完整规则')+'，当前金额未修改。')
        return resolved


    def _quotation_customer_rows(self):
        """An explicit customer-document whitelist; no classification or internal cost fields."""
        self.ensure_one()
        self.check_access("read")
        if any(line.selection_pricing_result and (line.saved_result_state != "calculated") for line in self.order_line.sudo()):
            raise UserError("当前报价参数已变化或计算未完成，请先重新计算，再预览或下载。")
        rows, number = [], 0
        for line in self._get_order_lines_to_report():
            if line.display_type:
                rows.append({"display_type": line.display_type, "name": line.name or ""})
                continue
            number += 1
            rows.append({"display_type": False, "number": number, "name": line.quotation_name or "",
                "specification": line.quotation_specification or "", "quantity": line.product_uom_qty,
                "unit": "组" if len((line.selection_values or {}).get("cabinet_configuration", {}).get("cabinets", [])) > 1 else line.product_uom_id.display_name or "", "unit_price": line.quotation_discounted_unit,
                "total": line.quotation_discounted_total, "remark": line.quotation_remark or "",
                "unit_price_text": format_amount(self.env, line.quotation_discounted_unit, self.currency_id),
                "total_text": format_amount(self.env, line.quotation_discounted_total, self.currency_id)})
        return rows

    def action_download_customer_xlsx(self):
        self.ensure_one()
        self.check_access("read")
        return {"type": "ir.actions.act_url", "url": f"/sale/customer-quotation/{self.id}/xlsx", "target": "download"}

    def _customer_quotation_xlsx(self):
        """Write the same eight whitelisted columns as the customer PDF and portal preview."""
        import io
        import xlsxwriter
        self.ensure_one()
        self.check_access("read")
        stream = io.BytesIO()
        book = xlsxwriter.Workbook(stream, {"in_memory": True})
        sheet = book.add_worksheet("报价单")
        common = {"font_name": "Microsoft YaHei", "font_size": 11, "valign": "top", "text_wrap": True}
        text = book.add_format(common)
        heading = book.add_format({**common, "bold": True, "bottom": 1})
        amount = book.add_format({**common, "num_format": "#,##0.00"})
        title = book.add_format({**common, "font_size": 20, "align": "center"})
        sheet.merge_range(0, 0, 0, 7, "报价单", title)
        sheet.merge_range(2, 0, 2, 3, "买方：" + self.partner_id.display_name, text)
        sheet.merge_range(2, 4, 2, 7, "卖方：" + self.company_id.name, text)
        sheet.merge_range(3, 0, 3, 3, "编号：" + self.name, text)
        sheet.merge_range(3, 4, 3, 7, "日期：" + fields.Date.to_string(self.date_order.date()), text)
        sheet.merge_range(4, 0, 4, 7, "付款条款：" + (self.payment_term_id.display_name or ""), text)
        sheet.merge_range(5, 0, 5, 7, "交付日期：" + (fields.Date.to_string(self.commitment_date.date()) if self.commitment_date else ""), text)
        headers = ("序号", "名称", "规格型号（W×D×H）", "数量", "单位", "折后单价", "折后总价", "备注")
        for col, label in enumerate(headers): sheet.write(7, col, label, heading)
        position, total = 8, 0
        for row in self._quotation_customer_rows():
            if row["display_type"]:
                sheet.merge_range(position, 0, position, 7, row["name"], heading)
            else:
                values = [row["number"], row["name"], row["specification"], row["quantity"], row["unit"], row["unit_price"], row["total"], row["remark"]]
                for col, value in enumerate(values):
                    if isinstance(value, (int, float)): sheet.write_number(position, col, value, amount if col in (5, 6) else text)
                    else: sheet.write_string(position, col, value, text)
                total += row["total"]
                sheet.set_row(position, max(54, 15 * (1 + len(row["remark"]) // 32 + row["remark"].count("\n"))))
            position += 1
        sheet.merge_range(position, 0, position, 5, "合计", heading)
        sheet.write_number(position, 6, total, amount)
        for col, width in enumerate((7, 18, 26, 10, 9, 16, 18, 52)): sheet.set_column(col, col, width)
        sheet.freeze_panes(8, 0)
        sheet.set_landscape(); sheet.fit_to_pages(1, 0); sheet.repeat_rows(7)
        book.close()
        return stream.getvalue()

    def calculate_selection_quote(self):
        self._check_selection_table_editable()
        self.env.cr.execute("SELECT id FROM sale_order WHERE id = %s FOR UPDATE", [self.id])
        self.invalidate_recordset()
        if not self.selection_price_settings:
            self.selection_price_settings = self._selection_pricing_defaults()
        data = self.get_selection_table()
        rows = [row for row in data["rows"] if not row.get("display_type")]
        if not rows:
            return {"ok": False, "issues": [], "message": "请先添加产品或上传图纸。"}
        issues = self._selection_quote_issues(rows)
        if issues:
            return {"ok": False, "issues": issues,
                    "message": f"还有 {len(issues)} 个产品需要完善，暂未更新报价明细。"}
        before = self._selection_quote_signature()
        try:
            prices = self._calculate_selection_prices(rows)
            if self._selection_quote_signature() != before:
                raise ValidationError("产品参数已变化，请重新计算报价。")
            if not isinstance(prices, dict) or set(prices) != {row["line_id"] for row in rows}:
                raise ValidationError("报价结果与产品行不一致，本次未更新报价明细。")
            for row in rows:
                result = prices[row["line_id"]]
                value = result.get("price_unit") if isinstance(result, dict) else result
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValidationError("报价返回了无效单价，本次未更新报价明细。")
                if isinstance(result, dict) and (not result.get("item", {}).get("quote_cost_display") or not result.get("response", {}).get("quote_line_id")):
                    raise ValidationError("报价缺少完整计算快照，本次未更新报价明细。")
        except (UserError, ValidationError) as error:
            message = str(error)
            self.selection_pricing_error = message
            self.order_line.filtered(lambda l: not l.display_type).write({"selection_pricing_error": message})
            return {"ok": False, "issues": [], "message": message}
        now = fields.Datetime.to_string(fields.Datetime.now())
        for ordinal, row in enumerate(rows, 1):
            current = self.order_line.filtered(lambda l: l.id == row["line_id"])
            result = prices[row["line_id"]]
            if not isinstance(result, dict):
                current.write({"price_unit": result})
                continue
            coefficient = result["item"]["quick_discount"]
            selection = {**(current.selection_values or {}), "quote_discount_coefficient": coefficient}
            # The original rule has already applied the selective discount.
            # Store the final unit price, with no second native line discount.
            current.write({"price_unit": result["price_unit"], "discount": 0,
                           "selection_values": selection, "selection_pricing_error": False})
            case = {"scheme_no": self.name, "version": "实时", "provenance": "current_pricing_service",
                    "source_path": result["response"]["quote_line_id"]}
            snapshot = current._saved_result_from_item(case, result["item"], ordinal, now)
            snapshot.update(service_response=result["response"], request=result["request"],
                catalog_version=result["catalog_version"], matcher_sha256=result["matcher_sha256"],
                original_rule_sha256=result["source_sha256"], account=result["account"],
                resolved_risks=result.get("resolved_risks", []), kind="current_calculation")
            current.selection_pricing_result = snapshot
        self.selection_pricing_error = False
        self.selection_quote_snapshot = {"signature": self._selection_quote_signature(),
            "calculated_at": now, "total": self.amount_total, "currency_id": self.currency_id.id,
            "count": len(rows), "source": "original_quote_service"}
        return {"ok": True, "calculation": self.get_selection_table()["calculation"]}

    drawing_recognition_pending = fields.Boolean(compute="_compute_drawing_recognition_progress")
    drawing_recognition_version = fields.Char(compute="_compute_drawing_recognition_progress")

    @api.depends("drawing_ids.recognition_state", "drawing_ids.recognition_token", "drawing_ids.recognition_completed_pages", "drawing_ids.state", "drawing_ids.orientation_state", "drawing_ids.orientation_data")
    def _compute_drawing_recognition_progress(self):
        for order in self:
            order.drawing_recognition_pending = any(
                (drawing.recognition_state in ("queued", "processing") or drawing.orientation_state in ("queued","processing")) for drawing in order.drawing_ids
            )
            order.drawing_recognition_version = ";".join(
                f"{drawing.id}:{drawing.recognition_state}:{drawing.recognition_token}:{drawing.recognition_completed_pages}:{drawing.orientation_state}:{len((drawing.orientation_data or {}).get("pages",{}))}:{drawing.state}"
                for drawing in order.drawing_ids
            )

    @api.readonly
    def get_drawing_recognition_progress(self):
        self.ensure_one()
        self.check_access("read")
        return {"version": self.drawing_recognition_version}

    def action_upload_drawing(self):
        self.ensure_one()
        self.check_access("write")
        if self.state not in ("draft", "sent") or self.quotation_status == "contract" or self.locked:
            raise UserError("请在可编辑的报价单中上传图纸。")
        if not self.drawing_ids and self.order_line.filtered(lambda line: not line.display_type):
            raise UserError("当前报价已添加无图纸产品，请在新的报价单中上传图纸。")
        return {
            "name": "上传图纸",
            "type": "ir.actions.act_window",
            "res_model": "sale.order.drawing",
            "views": [(self.env.ref("sale.sale_order_drawing_upload_form").id, "form")],
            "target": "new",
            "context": {**self.env.context, "default_order_id": self.id},
        }

    def _check_selection_table_editable(self):
        self.ensure_one()
        self.check_access("write")
        self.env["sale.order.drawing"]._check_orders_editable(self)

    def _selection_line_for_candidate(self, candidate):
        """Reuse an unambiguous prior result when the same drawing is recognized again."""
        self.ensure_one()
        direct = self.order_line.filtered(lambda line: line.selection_candidate_id == candidate)[:1]
        if direct:
            return direct
        previous = self.order_line.filtered(lambda line:
            line.selection_drawing_id == candidate.drawing_id
            and line.selection_candidate_id
            and line.selection_candidate_id.recognition_token != candidate.recognition_token
            and line.selection_candidate_id.page_number == candidate.page_number
            and line.selection_candidate_id.name == candidate.name)
        return previous if len(previous) == 1 else self.env["sale.order.line"]

    @api.readonly
    def get_selection_table(self):
        """One product table, backed by native sale lines and recognition candidates."""
        self.ensure_one()
        self.check_access("read")
        rows, used = [], set()
        names = self.env["sale.order.drawing"]._selection_field_names()

        def line_row(line):
            values = dict(line.selection_values or {})
            series = line.product_template_id.jingneng_catalog_series
            return {**values, "key": f"line_{line.id}", "line_id": line.id,
                    "product_id": line.product_id.id,
                    "product_name": line.product_id.with_context(display_default_code=False).display_name,
                    "product_series": values.get("product_series") or series or "",
                    "quantity": line.product_uom_qty, "display_type": line.display_type,
                    "text": line.name, "source": "drawing" if line.selection_drawing_id else "manual",
                    "drawing_id": line.selection_drawing_id.id,
                    "independent": bool(line.selection_drawing_id and (
                        line.selection_candidate_id and line.selection_candidate_id.recognition_token != line.selection_drawing_id.recognition_token
                        or not line.selection_candidate_id and line.selection_drawing_id.order_line_id != line)),
                    "candidate_id": line.selection_candidate_id.id, "state": "confirmed"}

        for drawing in self.drawing_ids.sorted("id"):
            candidates = drawing._current_recognition_candidates().filtered(lambda c: not c.excluded_from_quote)
            for candidate in candidates:
                line = self._selection_line_for_candidate(candidate)
                values = candidate.review_values or candidate.suggested_values or {}
                if drawing.active_recognition_id == candidate:
                    values = {name: drawing[name] for name in names
                              if name not in ("product_template_id",)}
                row = {**values, "key": f"candidate_{candidate.id}", "source": "drawing",
                       "product_name": dict(DRAWING_PRODUCT_SERIES).get(values.get("product_series"), "待选型"), "drawing_id": drawing.id,
                       "candidate_id": candidate.id, "line_id": line.id, "product_id": line.product_id.id,
                       "page": candidate.page_number, "state": candidate.review_state,
                       "recognition_state": drawing.recognition_state}
                if line:
                    used.add(line.id)
                    if line.selection_candidate_id == candidate:
                        row.update(line_row(line), key=f"candidate_{candidate.id}",
                                   recognition_state=drawing.recognition_state, page=candidate.page_number,
                                   state=candidate.review_state)
                rows.append(row)
            if not drawing._current_recognition_candidates() and not drawing.selection_row_hidden:
                line = drawing.order_line_id
                if line:
                    used.add(line.id)
                values = {name: drawing[name] for name in names if name != "product_template_id"}
                rows.append({**values, "key": f"drawing_{drawing.id}", "source": "drawing",
                             "drawing_id": drawing.id, "candidate_id": False,
                             "line_id": line.id, "product_id": line.product_id.id,
                             "product_name": line.product_id.with_context(display_default_code=False).display_name or (
                                 dict(DRAWING_PRODUCT_SERIES).get(drawing.product_series) or "待识别产品"),
                             "state": drawing.state, "recognition_state": drawing.recognition_state,
                             "placeholder": not bool(drawing.product_series or line)})
        for line in self.order_line.sorted("sequence"):
            if line.id not in used:
                rows.append(line_row(line))
        attachments = [{"id": drawing.id, "name": drawing.name, "pages": drawing.page_count,
                        "kind": drawing.file_kind,
                        "recognition_state": drawing.recognition_state,
                        "error": drawing.recognition_error or ""} for drawing in self.drawing_ids.sorted("id")]
        rows = [_normalize_cabinet_values(row) if not row.get("display_type") else row for row in rows]
        snapshot = self.selection_quote_snapshot or {}
        calculation = {"calculated": bool(snapshot),
                       "stale": bool(snapshot and snapshot.get("signature") != self._selection_quote_signature()),
                       "count": snapshot.get("count", 0),
                       "total": format_amount(self.env, snapshot.get("total", 0),
                                              self.env["res.currency"].browse(snapshot.get("currency_id")) or self.currency_id)}
        return {"rows": rows, "attachments": attachments, "calculation": calculation,
                "drawing": bool(self.drawing_ids),
                "total": format_amount(self.env, self.amount_total, self.currency_id),
                "editable": self.state in ("draft", "sent") and self.quotation_status != "contract" and not self.locked}

    @api.readonly
    def get_selection_accessory_catalog(self):
        self.ensure_one()
        self.check_access("read")
        return list(self.env["sale.order.drawing"]._accessory_catalog_for_company(self.company_id).values())

    def save_selection_line(self, payload):
        return self._save_selection_line(payload)

    def _save_selection_line(self, payload, from_drawing=False):
        self._check_selection_table_editable()
        if not isinstance(payload, dict):
            raise ValidationError("产品参数无效，请重新填写。")
        line = self.env["sale.order.line"].browse(int(payload.get("line_id") or 0)).exists()
        if line and line.order_id != self:
            raise AccessError("只能修改当前报价的产品。")
        display_type = payload.get("display_type") or False
        if display_type and display_type not in ("line_section", "line_note"):
            raise ValidationError("请选择有效的分组或备注。")
        if display_type:
            vals = {"name": str(payload.get("text") or ""), "display_type": display_type}
        else:
            product = self.env["product.product"].browse(int(payload.get("product_id") or 0)).exists()
            if not product or not product.sale_ok:
                raise ValidationError("请先选择可以销售的产品。")
            product.check_access("read")
            if (not line or product != line.product_id) and not product.filtered_domain(
                self.env["sale.order.line"]._domain_product_id()
            ):
                raise ValidationError("产品请选择机箱机柜目录中的 11 个系列；附件请在附件区域选择。")
            if product.company_id and product.company_id != self.company_id:
                raise ValidationError("请选择当前公司的产品。")
            allowed = self.env["sale.order.drawing"]._selection_field_names()
            values = dict(line.selection_values or {})
            values.update({name: payload[name] for name in allowed if name in payload
                           and name not in ("product_template_id", "dimension_assist_data")})
            accessory_data = values.get("accessory_selection_data")
            if accessory_data:
                self.env["sale.order.drawing"]._validate_accessory_selection_data(accessory_data)
                catalog = self.env["sale.order.drawing"]._accessory_catalog_for_company(self.company_id)
                previous = (line.selection_values or {}).get("accessory_selection_data") or {}
                previous_names = set(previous.get("choices", {})) | set(previous.get("manual_names", []))
                selected_names = {name for name, selected in accessory_data["choices"].items() if selected}
                if (selected_names | set(accessory_data.get("manual_names", []))) - set(catalog) - previous_names:
                    raise ValidationError("附件已不在当前附件库，请重新选择。")
            values["product_series"] = self.env["sale.order.drawing"]._series_from_product(product.product_tmpl_id)
            for key in ("quantity", "width_mm", "depth_mm", "height_mm", "thickness_mm"):
                try:
                    value = float(values.get(key) or 0)
                except (TypeError, ValueError):
                    raise ValidationError("数量、尺寸和料厚请填写有效数字。") from None
                if not math.isfinite(value) or value < 0 or (key == "quantity" and value <= 0):
                    raise ValidationError("数量必须大于零，尺寸和料厚不能为负数。")
                values[key] = value
            if "width_mm" in payload and "width_expression" not in payload:
                values["width_expression"] = f"{values['width_mm']:g}" if values["width_mm"] else ""
            values = _normalize_cabinet_values(values)
            if values.get("product_series") and values["product_series"] not in dict(DRAWING_PRODUCT_SERIES):
                raise ValidationError("请选择已有产品系列。")
            if not line and self.drawing_ids and not from_drawing:
                raise UserError("当前为图纸报价，请通过图纸选型添加产品。")
            vals = {"selection_values": values}
            if not line or line.product_id != product:
                vals["product_id"] = product.id
            if not line or line.product_uom_qty != values["quantity"]:
                vals["product_uom_qty"] = values["quantity"]
            if line and line.display_type:
                raise ValidationError("分组或备注不能转换为产品行。")
        if line:
            line.write(vals)
        else:
            line = self.env["sale.order.line"].create({"order_id": self.id, **vals})
        if not display_type:
            line._quotation_refresh_automatic_remark()
        return {"line_id": line.id,
                "product_name": line.product_id.with_context(display_default_code=False).display_name,
                "product_series": (line.selection_values or {}).get("product_series") or "",
                "values": dict(line.selection_values or {}),
                "total": format_amount(self.env, self.amount_total, self.currency_id)}

    def copy_selection_line(self, line_id):
        self._check_selection_table_editable()
        line = self.order_line.filtered(lambda l: l.id == line_id)
        if not line or line.display_type:
            raise UserError("请先保存产品后再复制。")
        copy = line.copy({"order_id": self.id, "selection_candidate_id": False,
                          "selection_drawing_id": line.selection_drawing_id.id})
        return {"line_id": copy.id}

    def remove_selection_row(self, line_id=0, drawing_id=0, candidate_id=0):
        self._check_selection_table_editable()
        if candidate_id:
            candidate = self.env["sale.order.drawing.candidate"].browse(candidate_id).exists()
            if not candidate or candidate.drawing_id.order_id != self:
                raise AccessError("识别项目不属于当前报价。")
            candidate.sudo().write({"excluded_from_quote": True})
        if line_id:
            line = self.order_line.filtered(lambda l: l.id == line_id)
            if not line:
                raise AccessError("产品不属于当前报价。")
            line.unlink()
        elif drawing_id and not candidate_id:
            drawing = self.drawing_ids.filtered(lambda d: d.id == drawing_id)
            if not drawing:
                raise AccessError("图纸不属于当前报价。")
            drawing.write({"selection_row_hidden": True})
        return True

    def open_selection_row(self, drawing_id, candidate_id=0):
        self.ensure_one()
        self.check_access("read")
        drawing = self.drawing_ids.filtered(lambda d: d.id == drawing_id)
        if not drawing:
            raise AccessError("图纸不属于当前报价。")
        if candidate_id and drawing.active_recognition_id.id != candidate_id:
            candidate = drawing._current_recognition_candidates().filtered(lambda c: c.id == candidate_id)
            if not candidate or candidate.excluded_from_quote:
                raise UserError("此识别项目已更新，请刷新后重试。")
            self._check_selection_table_editable()
            drawing._save_candidate_selection()
            drawing.write({"active_recognition_id": candidate.id})
            drawing.action_apply_recognition()
            if candidate.review_state == "confirmed":
                super(SaleOrderDrawing, drawing).write({"state": "confirmed"})
        return drawing.action_open_selection()

    def run_selection_recognition(self, drawing_id):
        self._check_selection_table_editable()
        drawing = self.drawing_ids.filtered(lambda d: d.id == drawing_id)
        if not drawing:
            raise AccessError("图纸不属于当前报价。")
        drawing.action_recognize()
        return True

    def remove_selection_attachment(self, drawing_id):
        self._check_selection_table_editable()
        drawing = self.drawing_ids.filtered(lambda d: d.id == drawing_id)
        if not drawing:
            raise AccessError("图纸不属于当前报价。")
        drawing.unlink()
        return True

    name = fields.Char(
        string="Order Reference",
        required=True,
        copy=False,
        readonly=False,
        index="trigram",
        default=lambda s: s.env._("New"),
    )

    company_id = fields.Many2one(
        comodel_name="res.company", required=True, index=True, default=lambda self: self.env.company
    )
    partner_id = fields.Many2one(
        comodel_name="res.partner",
        string="Customer",
        required=True,
        change_default=True,
        index=True,
        tracking=1,
        check_company=True,
    )
    state = fields.Selection(
        selection=SALE_ORDER_STATE,
        string="Status",
        readonly=True,
        copy=False,
        index=True,
        tracking=3,
        group_expand=True,
        default="draft",
    )
    quotation_workflow_step = fields.Selection(
        [("quotation", "报价单"), ("contract", "合同")],
        string="报价处理阶段",
        copy=False,
        readonly=True,
    )
    quotation_status = fields.Selection(
        [
            ("draft", "草稿"),
            ("quotation", "报价单"),
            ("sent", "已发送报价单"),
            ("contract", "合同"),
            ("sale", "销售订单"),
            ("cancel", "已取消"),
        ],
        string="状态",
        compute="_compute_quotation_status",
        store=True,
        index=True,
        group_expand=True,
    )

    # Customer feedback is independent of contract/order/cancellation workflow.
    quotation_first_sent_at = fields.Datetime(string="首次正式发送日期", copy=False, tracking=True, index=True)
    quotation_result = fields.Selection([
        ('pending', '待反馈'), ('won', '中标'), ('lost', '失标')],
        string="客户反馈结果", default='pending', required=True, copy=False, tracking=True)
    quotation_feedback_date = fields.Date(string="客户反馈日期", copy=False, tracking=True)
    quotation_lost_reason = fields.Selection([
        ('price', '价格原因'), ('delivery', '交期不符'), ('technical', '技术不符'),
        ('terminated', '客户终止项目'), ('competitor', '选择其他供应商'),
        ('other', '其他'), ('unrecorded', '未登记')], string="失标原因", copy=False, tracking=True)
    quotation_feedback_notes = fields.Text(string="反馈备注", copy=False, tracking=True)
    quotation_followup_date = fields.Date(string="下次跟进日期", copy=False, tracking=True)
    quotation_revision_of_id = fields.Many2one('sale.order', string="上一版报价", copy=False,
        check_company=True, ondelete='restrict', help="仅同一业务的重新报价关联上一版；普通复制不视为同一报价。")

    @api.constrains('quotation_revision_of_id', 'partner_id', 'company_id')
    def _check_quotation_revision(self):
        for order in self:
            seen = {order.id}
            parent = order.quotation_revision_of_id
            while parent:
                parent.check_access('read')
                if parent.id in seen:
                    raise ValidationError('上一版报价不能形成循环。')
                if parent.company_id != order.company_id or parent.partner_id != order.partner_id:
                    raise ValidationError('同一业务的报价版本必须属于同一公司和同一客户。')
                seen.add(parent.id)
                parent = parent.quotation_revision_of_id

    @api.constrains('quotation_result', 'quotation_feedback_date', 'quotation_first_sent_at')
    def _check_quotation_feedback(self):
        now = fields.Datetime.now()
        today = fields.Date.context_today(self)
        for order in self:
            if order.quotation_first_sent_at and order.quotation_first_sent_at > now:
                raise ValidationError('首次正式发送日期不能晚于当前时间。')
            if order.quotation_result in ('won', 'lost'):
                if not order.quotation_first_sent_at or not order.quotation_feedback_date:
                    raise ValidationError('登记中标或失标前，请填写真实的首次正式发送日期和客户反馈日期。')
                first_day = fields.Datetime.context_timestamp(order, order.quotation_first_sent_at).date()
                if not first_day <= order.quotation_feedback_date <= today:
                    raise ValidationError('客户反馈日期应在首次发送日期至今天之间。')

    def action_open_customer_feedback(self):
        self.ensure_one()
        if not self.quotation_first_sent_at:
            raise UserError('请先将报价单发送给客户，再登记客户反馈结果。')
        return {'name': '客户反馈', 'type': 'ir.actions.act_window', 'res_model': 'sale.order',
                'res_id': self.id, 'views': [(self.env.ref('sale.sale_order_customer_feedback_form').id, 'form')],
                'target': 'new'}

    quotation_source_date = fields.Date(string="原报价日期", copy=False, readonly=True)
    quotation_prepared_at = fields.Datetime(string="转为报价单时间", copy=False, readonly=True)
    quotation_sent_at = fields.Datetime(string="发送报价时间", copy=False, readonly=True)
    quotation_contract_at = fields.Datetime(string="进入合同时间", copy=False, readonly=True)
    quotation_confirmed_at = fields.Datetime(string="确认销售订单时间", copy=False, readonly=True)
    locked = fields.Boolean(
        help="Locked orders cannot be modified.", default=False, copy=False, tracking=True
    )
    invoicing_closed = fields.Boolean(
        string="Manually Closed For Invoicing",
        help="If enabled, the order is considered fully invoiced regardless of lines.",
        copy=False,
    )
    has_archived_products = fields.Boolean(compute="_compute_has_archived_products")

    client_order_ref = fields.Char(string="Customer Reference", copy=False)
    create_date = fields.Datetime(  # Override of default create_date field from ORM
        string="Creation Date", index=True, readonly=True
    )
    commitment_date = fields.Datetime(
        string="Promised Delivery",
        copy=False,
        help="This is the delivery date promised to the customer. "
        "If set, the delivery order will be scheduled based on "
        "this date rather than product lead times.",
    )
    delivery_date = fields.Datetime(string="Delivery Date", compute="_compute_delivery_date")
    date_order = fields.Datetime(
        string="Order Date",
        required=True,
        copy=False,
        help="Creation date of draft/sent orders,\nConfirmation date of confirmed orders.",
        default=fields.Datetime.now,
    )
    origin = fields.Char(
        string="Source Document",
        help="Reference of the document that generated this sales order request",
    )
    reference = fields.Char(
        string="Payment Ref.", help="The payment communication of this sale order.", copy=False
    )
    pending_email_template_id = fields.Many2one(
        string="Pending Email Template",
        comodel_name="mail.template",
        ondelete="set null",
        readonly=True,
    )  # The template of the pending email that must be sent asynchronously.

    require_signature = fields.Boolean(
        string="Online signature",
        compute="_compute_require_signature",
        store=True,
        readonly=False,
        precompute=True,
        help="Request a online signature from the customer to confirm the order.",
    )
    prepayment_percent = fields.Float(
        string="Prepayment",
        compute="_compute_prepayment_percent",
        store=True,
        readonly=False,
        precompute=True,
        help="The percentage of the amount that must be paid by the customer to confirm the order.",
    )
    prepayment_amount = fields.Monetary(
        string="Prepayment Amount",
        help="The amount that must be paid by the customer to confirm the order.",
        currency_field="currency_id",
        compute="_compute_prepayment_amount",
        inverse="_inverse_prepayment_amount",
        readonly=False,
    )

    signature = fields.Image(
        string="Signature", copy=False, attachment=True, max_width=1024, max_height=1024
    )
    signed_by = fields.Char(string="Signed By", copy=False)
    signed_on = fields.Datetime(string="Signed On", copy=False)

    validity_date = fields.Date(
        string="Expiration",
        compute="_compute_validity_date",
        store=True,
        copy=False,
        precompute=True,
        exportable=False,
    )
    journal_id = fields.Many2one(
        string="Journal",
        comodel_name="account.journal",
        compute="_compute_journal_id",
        store=True,
        readonly=False,
        precompute=True,
        domain=[("type", "=", "sale")],
        check_company=True,
        help="If set, the SO will invoice in this journal; "
        "otherwise the sales journal with the lowest sequence is used.",
    )
    document_tax_mode = fields.Selection(
        selection=[("tax_excluded", "Tax Excl."), ("tax_included", "Tax Incl.")],
        compute="_compute_document_tax_mode",
        precompute=True,
        store=True,
        readonly=False,
        required=True,
    )

    # Partner-based computes
    note = fields.Html(
        string="Terms and conditions",
        compute="_compute_note",
        inverse="_inverse_note",
        store=True,
        readonly=False,
        precompute=True,
    )

    partner_invoice_id = fields.Many2one(
        comodel_name="res.partner",
        string="Invoice Address",
        compute="_compute_partner_invoice_id",
        store=True,
        readonly=False,
        required=True,
        precompute=True,
        check_company=True,
        index="btree_not_null",
    )
    partner_shipping_id = fields.Many2one(
        comodel_name="res.partner",
        string="Delivery Address",
        compute="_compute_partner_shipping_id",
        store=True,
        readonly=False,
        required=True,
        precompute=True,
        check_company=True,
        index="btree_not_null",
    )

    fiscal_position_id = fields.Many2one(
        comodel_name="account.fiscal.position",
        string="Fiscal Position",
        compute="_compute_fiscal_position_id",
        store=True,
        readonly=False,
        precompute=True,
        check_company=True,
        help="Fiscal positions are used to adapt taxes and accounts for particular customers or"
        " sales orders/invoices. The default value comes from the customer.",
    )
    payment_term_id = fields.Many2one(
        comodel_name="account.payment.term",
        string="Payment Terms",
        compute="_compute_payment_term_id",
        store=True,
        readonly=False,
        precompute=True,
        check_company=True,  # Unrequired company
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )
    preferred_payment_method_line_id = fields.Many2one(
        comodel_name="account.payment.method.line",
        string="Payment Method",
        compute="_compute_preferred_payment_method_line_id",
        store=True,
        precompute=True,
        readonly=False,
        check_company=True,
        domain="[('payment_type', '=', 'inbound'), ('company_id', '=', company_id)]",
    )
    pricelist_id = fields.Many2one(
        comodel_name="product.pricelist",
        string="Pricelist",
        compute="_compute_pricelist_id",
        store=True,
        readonly=False,
        precompute=True,
        check_company=True,  # Unrequired company
        tracking=1,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="If you change the pricelist, the prices of all order lines will be updated"
        " accordingly.",
    )
    currency_id = fields.Many2one(
        comodel_name="res.currency",
        compute="_compute_currency_id",
        store=True,
        precompute=True,
        ondelete="restrict",
    )
    currency_rate = fields.Float(
        string="Currency Rate",
        compute="_compute_currency_rate",
        digits=0,
        store=True,
        precompute=True,
    )
    user_id = fields.Many2one(
        comodel_name="res.users",
        string="Salesperson",
        compute="_compute_user_id",
        store=True,
        readonly=False,
        precompute=True,
        index=True,
        tracking=2,
        domain=lambda self: (
            "[('all_group_ids', 'in', {}), ('share', '=', False), ('company_ids', '=', company_id)]".format(  # noqa: E501
                self.env.ref("sales_team.group_sale_salesman").ids
            )
        ),
    )
    team_id = fields.Many2one(
        comodel_name="crm.team",
        string="Sales Team",
        compute="_compute_team_id",
        store=True,
        readonly=False,
        precompute=True,
        ondelete="set null",
        change_default=True,
        check_company=True,  # Unrequired company
        tracking=True,
        index=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )
    incoterm = fields.Many2one(
        comodel_name="account.incoterms",
        string="Incoterm",
        compute="_compute_incoterm",
        precompute=True,
        store=True,
        readonly=False,
        help="International Commercial Terms are a series of predefined commercial terms used in"
        " international transactions.",
    )
    incoterm_location = fields.Char(
        string="Incoterm Location",
        compute="_compute_incoterm_location",
        precompute=True,
        store=True,
        readonly=False,
    )

    # Lines and line based computes
    order_line = fields.One2many(
        comodel_name="sale.order.line",
        inverse_name="order_id",
        string="Order Lines",
        copy=True,
        bypass_search_access=True,
    )

    amount_untaxed = fields.Monetary(
        string="Untaxed Amount", store=True, compute="_compute_amounts", tracking=5
    )
    amount_tax = fields.Monetary(string="Taxes", store=True, compute="_compute_amounts")
    amount_total = fields.Monetary(
        string="Total", store=True, compute="_compute_amounts", tracking=4
    )
    amount_to_invoice = fields.Monetary(
        string="Un-invoiced Balance", compute="_compute_amount_to_invoice"
    )
    amount_invoiced = fields.Monetary(string="Already invoiced", compute="_compute_amount_invoiced")

    invoice_count = fields.Integer(string="Invoice Count", compute="_get_invoiced")
    invoice_ids = fields.Many2many(
        comodel_name="account.move",
        string="Invoices",
        compute="_get_invoiced",
        search="_search_invoice_ids",
        copy=False,
    )
    invoice_status = fields.Selection(
        selection=INVOICE_STATUS,
        string="Invoice Status",
        compute="_compute_invoice_status",
        store=True,
    )

    sale_warning_text = fields.Text(
        "Sale Warning",
        help="Internal warning for the partner or the products as set by the user.",
        compute="_compute_sale_warning_text",
    )
    show_sol_numbers = fields.Boolean(related="company_id.show_sol_numbers")

    # Payment fields
    transaction_count = fields.Integer(compute="_compute_transaction_count")
    transaction_ids = fields.Many2many(
        comodel_name="payment.transaction",
        relation="sale_order_transaction_rel",
        column1="sale_order_id",
        column2="transaction_id",
        string="Transactions",
        groups="account.group_account_invoice",
        copy=False,
        readonly=True,
    )
    authorized_transaction_ids = fields.Many2many(
        comodel_name="payment.transaction",
        string="Authorized Transactions",
        compute="_compute_authorized_transaction_ids",
        copy=False,
        groups="account.group_account_invoice",
        compute_sudo=True,
    )
    has_authorized_transaction_ids = fields.Boolean(
        string="Has Authorized Transactions",
        compute="_compute_authorized_transaction_ids",
        compute_sudo=True,
    )
    amount_paid = fields.Float(
        string="Payment Transactions Amount",
        help="Sum of transactions made in through the online payment form that are in the state"
        " 'done' or 'authorized' and linked to this order.",
        compute="_compute_amount_paid",
        compute_sudo=True,
    )
    amount_unpaid = fields.Monetary(
        string="Amount Remaining",
        help="Amount left to pay to avoid double payment or double invoicing.",
        compute="_compute_amount_unpaid",
        store=True,
    )

    # UTMs - enforcing the fact that we want to 'set null' when relation is unlinked
    campaign_id = fields.Many2one(ondelete="set null")
    medium_id = fields.Many2one(ondelete="set null")
    source_id = fields.Many2one(ondelete="set null")

    # Followup ?
    tag_ids = fields.Many2many(
        comodel_name="crm.tag",
        relation="sale_order_tag_rel",
        column1="order_id",
        column2="tag_id",
        groups="sales_team.group_sale_salesman",
        string="Tags",
    )

    # Remaining non stored computed fields (hide/make fields readonly, ...)
    country_code = fields.Char(
        related="company_id.account_fiscal_country_id.code", string="Country code"
    )
    company_price_include = fields.Selection(related="company_id.account_price_include")
    delivery_status = fields.Selection(
        [
            ("pending", "Not Delivered"),
            ("started", "Started"),
            ("partial", "Partially Delivered"),
            ("full", "Fully Delivered"),
        ],
        string="Delivery Status",
        compute="_compute_delivery_status",
        store=True,
        help="Blue: Not Delivered/Started\n\
            Orange: Partially Delivered\n\
            Green: Fully Delivered",
    )
    duplicated_order_ids = fields.Many2many(
        comodel_name="sale.order", compute="_compute_duplicated_order_ids"
    )
    expected_date = fields.Datetime(
        string="Expected Date",
        compute="_compute_expected_date",
        store=False,  # Note: can not be stored since depends on today()
        help="Delivery date you can promise to the customer, computed from the minimum lead time of"
        " the order lines.",
    )
    extra_total_fields = fields.Json(compute="_compute_extra_total_fields")
    is_expired = fields.Boolean(
        string="Is Expired", compute="_compute_is_expired", search="_search_is_expired"
    )
    is_unfulfilled = fields.Boolean(
        string="Unfulfilled Orders", search="_search_is_unfulfilled", store=False
    )
    partner_credit_warning = fields.Text(compute="_compute_partner_credit_warning")
    show_ship_button = fields.Boolean(compute="_compute_show_ship_button")
    tax_calculation_rounding_method = fields.Selection(
        related="company_id.tax_calculation_rounding_method", depends=["company_id"]
    )
    tax_country_id = fields.Many2one(
        comodel_name="res.country",
        compute="_compute_tax_country_id",
        # Avoid access error on fiscal position when reading a sale order with company != user ones
        compute_sudo=True,
    )
    tax_totals = fields.Json(compute="_compute_tax_totals", exportable=False)
    terms_type = fields.Selection(related="company_id.terms_type")
    type_name = fields.Char(string="Type Name", compute="_compute_type_name")

    has_overages = fields.Boolean(compute="_compute_has_overages")
    invoice_overages = fields.Boolean()

    # Remaining ux fields (not computed, not stored)

    has_active_pricelist = fields.Boolean(compute="_compute_has_active_pricelist")

    analytic_account_id = fields.Many2one(
        string="Analytic Account", comodel_name="account.analytic.account"
    )

    _date_order_id_idx = models.Index("(date_order desc, id desc)")

    # === COMPUTE METHODS ===#

    @api.depends("name", "partner_id", "client_order_ref")
    @api.depends_context("sale_show_partner_name", "formatted_display_name")
    def _compute_display_name(self):
        super()._compute_display_name()
        for order in self:
            order_label = order.name
            if order.client_order_ref:
                order_label = f"{order_label} ({order.client_order_ref})"

            if self.env.context.get("sale_show_partner_name") and order.partner_id.name:
                if self.env.context.get("formatted_display_name"):
                    order.display_name = f"{order_label} \t --{order.partner_id.name}--"
                else:
                    order.display_name = f"{order_label} - {order.partner_id.name}"
            else:
                order.display_name = order_label

    @api.depends("order_line.product_id")
    def _compute_has_archived_products(self):
        for order in self:
            order.has_archived_products = any(
                not product.active for product in order.order_line.product_id
            )

    @api.depends("company_id")
    def _compute_require_signature(self):
        for order in self:
            order.require_signature = order.company_id.portal_confirmation_sign

    @api.depends("company_id")
    def _compute_prepayment_percent(self):
        for order in self:
            if order.company_id.portal_confirmation_pay:
                order.prepayment_percent = order.company_id.prepayment_percent
            else:
                order.prepayment_percent = 0.0

    @api.depends("prepayment_percent", "amount_total")
    def _compute_prepayment_amount(self):
        for order in self:
            order.prepayment_amount = order.prepayment_percent * order.amount_total

    def _inverse_prepayment_amount(self):
        for order in self:
            if order.amount_total:
                order.prepayment_percent = order.prepayment_amount / order.amount_total
            else:
                order.prepayment_percent = 0.0

    def _compute_validity_date(self):
        self.validity_date = False

    def _compute_journal_id(self):
        self.journal_id = False

    @api.depends("partner_id", "company_id")
    def _compute_note(self):
        use_invoice_terms = (
            self.env["ir.config_parameter"].sudo().get_bool("account.use_invoice_terms")
        )
        if not use_invoice_terms:
            return
        for order in self:
            if order.state != "draft":
                continue
            order = order.with_company(order.company_id)
            if order.terms_type == "html" and order.env.company.invoice_terms_html:
                baseurl = html_keep_url(order._get_note_url() + "/terms")
                context = {"lang": order.partner_id.lang or self.env.user.lang}
                order.note = order.env._("Terms & Conditions: %s", baseurl)
                del context
            elif not is_html_empty(order.env.company.invoice_terms):
                if order.partner_id.lang:
                    order = order.with_context(lang=order.partner_id.lang)
                order.note = order.env.company.invoice_terms

    def _inverse_note(self):
        for sales_order in self:
            sales_order.note = self.env["account.move"]._set_links_to_new_tab(sales_order.note)

    @api.model
    def _get_note_url(self):
        return self.env.company.get_base_url()

    @api.depends("partner_id")
    def _compute_partner_invoice_id(self):
        for order in self:
            order.partner_invoice_id = (
                order.partner_id.address_get(["invoice"])["invoice"] if order.partner_id else False
            )

    @api.depends("partner_id")
    def _compute_partner_shipping_id(self):
        for order in self:
            order.partner_shipping_id = (
                order.partner_id.address_get(["delivery"])["delivery"]
                if order.partner_id
                else False
            )

    @api.depends("partner_shipping_id", "partner_id", "company_id")
    def _compute_fiscal_position_id(self):
        cache = {}
        for order in self:
            if not order.partner_id:
                order.fiscal_position_id = False
                continue
            key = (order.company_id.id, order.partner_id.id, order.partner_shipping_id.id)
            if key not in cache:
                cache[key] = (
                    self
                    .env["account.fiscal.position"]
                    .with_company(order.company_id)
                    ._get_fiscal_position(order.partner_id, order.partner_shipping_id)
                    .id
                )
            order.fiscal_position_id = cache[key]

    @api.depends("partner_id")
    def _compute_payment_term_id(self):
        for order in self:
            order = order.with_company(order.company_id)
            order.payment_term_id = order.partner_id.property_payment_term_id

    @api.depends("partner_id", "company_id")
    def _compute_preferred_payment_method_line_id(self):
        for order in self:
            order = order.with_company(order.company_id)
            order.preferred_payment_method_line_id = (
                order.partner_id.property_inbound_payment_method_line_id
            )

    @api.depends("partner_id", "company_id")
    def _compute_pricelist_id(self):
        for order in self:
            if order.state != "draft":
                continue
            if not order.partner_id:
                order.pricelist_id = False
                continue
            order = order.with_company(order.company_id)
            order.pricelist_id = order.partner_id.property_product_pricelist

    @api.depends("pricelist_id", "company_id")
    def _compute_currency_id(self):
        for order in self:
            order.currency_id = order.pricelist_id.currency_id or order.company_id.currency_id

    @api.depends("currency_id", "date_order", "company_id")
    def _compute_currency_rate(self):
        for order in self:
            order.currency_rate = self.env["res.currency"]._get_conversion_rate(
                from_currency=order.company_id.currency_id,
                to_currency=order.currency_id,
                company=order.company_id,
                date=(order.date_order or fields.Datetime.now()).date(),
            )

    @api.depends("company_id")
    def _compute_has_active_pricelist(self):
        for order in self:
            order.has_active_pricelist = bool(
                self.env["product.pricelist"].search(
                    [("company_id", "in", (False, order.company_id.id)), ("active", "=", True)],
                    limit=1,
                )
            )

    @api.depends("partner_id")
    def _compute_user_id(self):
        for order in self:
            if order.partner_id and not (order._origin.id and order.user_id):
                # Recompute the salesman on partner change
                #   * if partner is set (is required anyway, so it will be set sooner or later)
                #   * if the order is not saved or has no salesman already
                order.user_id = (
                    order.partner_id.user_id
                    or order.partner_id.commercial_partner_id.user_id
                    or (self.env.user.has_group("sales_team.group_sale_salesman") and self.env.user)
                )

    @api.depends("user_id")
    def _compute_team_id(self):
        cached_teams = {}
        for order in self:
            default_team_id = order._default_team_id()
            user_id = order.user_id.id
            company_id = order.company_id.id
            key = (default_team_id, user_id, company_id)
            if key not in cached_teams:
                cached_teams[key] = (
                    self
                    .env["crm.team"]
                    .with_context(default_team_id=default_team_id)
                    ._get_default_team_id(
                        user_id=user_id,
                        domain=self.env["crm.team"]._check_company_domain(company_id),
                    )
                )
            order.team_id = cached_teams[key]

    @api.depends("partner_id")
    def _compute_incoterm(self):
        for order in self:
            order.incoterm = order.partner_id.incoterm_id or order.company_id.incoterm_id

    @api.depends("partner_id")
    def _compute_incoterm_location(self):
        for order in self:
            order.incoterm_location = order.partner_id.incoterm_location

    def _default_team_id(self):
        return self.env.context.get("default_team_id", False) or self.team_id.id

    def _get_priced_lines(self):
        return self.order_line.filtered(lambda x: not x.display_type)

    @api.depends("order_line.price_subtotal", "currency_id", "company_id", "payment_term_id")
    def _compute_amounts(self):
        AccountTax = self.env["account.tax"]
        for order in self:
            order_lines = order._get_priced_lines()
            base_lines = [line._prepare_base_line_for_taxes_computation() for line in order_lines]
            base_lines += order._add_base_lines_for_early_payment_discount()
            AccountTax._add_tax_details_in_base_lines(base_lines, order.company_id)
            AccountTax._round_base_lines_tax_details(base_lines, order.company_id)
            tax_totals = AccountTax._get_tax_totals_summary(
                base_lines=base_lines,
                currency=order.currency_id or order.company_id.currency_id,
                company=order.company_id,
            )
            order.amount_untaxed = tax_totals["base_amount_currency"]
            order.amount_tax = tax_totals["tax_amount_currency"]
            order.amount_total = tax_totals["total_amount_currency"]

    def _add_base_lines_for_early_payment_discount(self):
        """Add the necessary lines in case of mixed early payment discount.

        When applying a payment term with an early payment discount, and when said payment term
        computes the tax on the 'mixed' setting, the tax computation is always based on the
        discounted amount untaxed.

        :returns: array containing the necessary lines (can be empty)
        :rtype: list
        """
        self.ensure_one()
        epd_lines = []
        if (
            self.payment_term_id.early_discount
            and self.payment_term_id.early_pay_discount_computation == "mixed"
            and self.payment_term_id.discount_percentage
        ):
            percentage = self.payment_term_id.discount_percentage
            currency = self.currency_id or self.company_id.currency_id
            for line in self._get_priced_lines():
                line_amount_after_discount = (line.price_subtotal / 100) * percentage
                epd_lines.append(
                    self.env["account.tax"]._prepare_base_line_for_taxes_computation(
                        record=self,
                        price_unit=-line_amount_after_discount,
                        quantity=1.0,
                        currency_id=currency,
                        sign=1,
                        special_mode="total_excluded",
                        special_type="early_payment",
                        tax_ids=line.tax_ids.flatten_taxes_hierarchy().filtered(
                            lambda tax: tax.amount_type != "fixed"
                        ),
                    )
                )
                epd_lines.append(
                    self.env["account.tax"]._prepare_base_line_for_taxes_computation(
                        record=self,
                        price_unit=line_amount_after_discount,
                        quantity=1.0,
                        currency_id=currency,
                        sign=1,
                        special_mode="total_excluded",
                        special_type="early_payment",
                    )
                )
        return epd_lines

    @api.depends("order_line.invoice_lines")
    def _get_invoiced(self):
        # The invoice_ids are obtained thanks to the invoice lines of the SO
        # lines, and we also search for possible refunds created directly from
        # existing invoices. This is necessary since such a refund is not
        # directly linked to the SO.
        for order in self:
            invoices = order.order_line.invoice_lines.move_id.filtered(
                lambda r: r.move_type in ("out_invoice", "out_refund")
            )
            order.invoice_ids = invoices
            order.invoice_count = len(invoices)

    @api.depends("transaction_ids")
    def _compute_transaction_count(self):
        if not self.env.user.has_group("account.group_account_invoice"):
            self.transaction_count = 0
            return
        for order in self:
            order.transaction_count = len(order.transaction_ids)

    def _search_invoice_ids(self, operator, value):
        if operator in Domain.NEGATIVE_OPERATORS:
            return NotImplemented
        if operator == "in" and value:
            falsy_domain = []
            if False in value:
                # special case for [('invoice_ids', '=', False)], i.e. "Invoices is not set"
                #
                # We cannot just search [('order_line.invoice_lines', '=', False)]
                # because it returns orders with uninvoiced lines, which is not
                # same "Invoices is not set" (some lines may have invoices and some
                # don't)
                #
                # A solution is using the 'not any' operators with inverted search first
                # ("orders with invoiced lines").
                falsy_domain = [
                    (
                        "order_line",
                        "not any",
                        [("invoice_lines.move_id.move_type", "in", ("out_invoice", "out_refund"))],
                    )
                ]
                if len(value) == 1:
                    return falsy_domain
            self.env.cr.execute(
                SQL(
                    """
                    SELECT array_agg(so.id)
                        FROM sale_order so
                        JOIN sale_order_line sol ON sol.order_id = so.id
                        JOIN sale_order_line_invoice_rel soli_rel ON soli_rel.order_line_id = sol.id
                        JOIN account_move_line aml ON aml.id = soli_rel.invoice_line_id
                        JOIN account_move am ON am.id = aml.move_id
                    WHERE
                        am.move_type in ('out_invoice', 'out_refund') AND
                        am.id = ANY(%s)
                    """,
                    list(value),
                )
            )
            so_ids = self.env.cr.fetchone()[0] or []
            return [("id", "in", so_ids)] + falsy_domain
        return [
            (
                "order_line.invoice_lines",
                "any",
                [
                    ("move_id.move_type", "in", ("out_invoice", "out_refund")),
                    ("move_id", operator, value),
                ],
            )
        ]

    @api.depends("state", "order_line.invoice_status", "invoicing_closed")
    def _compute_invoice_status(self):
        """
        Compute the invoice status of a SO. Possible statuses:
        - no: if the SO is not in status 'sale' or 'done', we consider that there is nothing to
          invoice. This is also the default value if the conditions of no other status is met.
        - to invoice: if any SO line is 'to invoice', the whole SO is 'to invoice'
        - invoiced: if all SO lines are invoiced, the SO is invoiced.
        - upselling: if all SO lines are invoiced or upselling, the status is upselling.
        """
        confirmed_orders = self.filtered(lambda so: so.state == "sale")
        (self - confirmed_orders).invoice_status = "no"
        closed_orders = confirmed_orders.filtered("invoicing_closed")
        closed_orders.invoice_status = "invoiced"
        orders_to_compute = confirmed_orders - closed_orders
        if not orders_to_compute:
            return
        lines_domain = [("is_downpayment", "=", False), ("display_type", "=", False)]
        line_invoice_status_all = [
            (order.id, invoice_status)
            for order, invoice_status in self.env["sale.order.line"]._read_group(
                lines_domain + [("order_id", "in", orders_to_compute.ids)],
                ["order_id", "invoice_status"],
            )
        ]
        for order in orders_to_compute:
            line_invoice_status = [d[1] for d in line_invoice_status_all if d[0] == order.id]
            if any(invoice_status == "to invoice" for invoice_status in line_invoice_status):
                if any(invoice_status == "no" for invoice_status in line_invoice_status):
                    # If only discount/delivery/promotion lines can be invoiced, the SO should not
                    # be invoiceable.
                    invoiceable_domain = lines_domain + [("invoice_status", "=", "to invoice")]
                    invoiceable_lines = order.order_line.filtered_domain(invoiceable_domain)
                    special_lines = invoiceable_lines.filtered(
                        lambda sol: not sol._can_be_invoiced_alone()
                    )
                    if invoiceable_lines == special_lines:
                        order.invoice_status = "no"
                    else:
                        order.invoice_status = "to invoice"
                else:
                    order.invoice_status = "to invoice"
            elif line_invoice_status and all(
                invoice_status == "invoiced" for invoice_status in line_invoice_status
            ):
                order.invoice_status = "invoiced"
            elif line_invoice_status and all(
                invoice_status in ("invoiced", "upselling")
                for invoice_status in line_invoice_status
            ):
                order.invoice_status = "upselling"
            else:
                order.invoice_status = "no"

    @api.depends("transaction_ids")
    def _compute_authorized_transaction_ids(self):
        for trans in self:
            trans.authorized_transaction_ids = trans.transaction_ids.filtered(
                lambda t: t.state == "authorized"
            )
            trans.has_authorized_transaction_ids = bool(trans.authorized_transaction_ids)

    @api.depends("transaction_ids.state", "transaction_ids.amount")
    def _compute_amount_paid(self):
        """Sum of the amount paid through all transactions for this SO."""
        for order in self:
            order.amount_paid = sum(
                tx.amount for tx in order.transaction_ids if tx.state in ("authorized", "done")
            )

    @api.depends("amount_total", "amount_paid", "amount_invoiced", "transaction_ids.invoice_ids")
    def _compute_amount_unpaid(self):
        for order in self:
            online_payments_invoices = order.transaction_ids.filtered(
                lambda tx: tx.state in {"authorized", "done"}
            ).invoice_ids
            offline_invoice_lines = order.order_line.invoice_lines.filtered(
                lambda line: (
                    line.parent_state in {"draft", "posted"}
                    and line.move_id not in online_payments_invoices
                )
            )
            offline_invoice_amount = sum(
                math.copysign(line.price_total, -line.balance) for line in offline_invoice_lines
            )

            order.amount_unpaid = max(
                order.amount_total - order.amount_paid - offline_invoice_amount, 0
            )

    def _get_advantages(self):
        """Compute advantages (tax included/excluded) for a sale order.

        :return: Tuple of advantage tax excluded and advantage tax included
        :rtype: tuple(float, float)
        """

        def grouping_function(base_line, tax_data):  # noqa: ARG001
            return base_line["special_type"] not in ("global_discount", "loyalty_discount")

        self.ensure_one()
        AccountTax = self.env["account.tax"]
        order_lines = self._get_priced_lines()

        # Original lines
        base_lines = [line._prepare_base_line_for_taxes_computation() for line in order_lines]
        AccountTax._add_tax_details_in_base_lines(base_lines, self.company_id)
        AccountTax._round_base_lines_tax_details(base_lines, self.company_id)
        base_lines_aggregated_values = AccountTax._aggregate_base_lines_tax_details(
            base_lines, grouping_function
        )
        values_per_grouping_key = AccountTax._aggregate_base_lines_aggregated_values(
            base_lines_aggregated_values
        )
        # Lines without discount, except if negative (=surcharge)
        base_lines_undiscounted = [
            line._prepare_base_line_for_taxes_computation(discount=min(line.discount, 0))
            for line in order_lines
        ]
        AccountTax._add_tax_details_in_base_lines(base_lines_undiscounted, self.company_id)
        AccountTax._round_base_lines_tax_details(base_lines_undiscounted, self.company_id)
        base_lines_undiscounted_aggregated_values = AccountTax._aggregate_base_lines_tax_details(
            base_lines_undiscounted, grouping_function
        )
        values_undiscounted_per_grouping_key = AccountTax._aggregate_base_lines_aggregated_values(
            base_lines_undiscounted_aggregated_values
        )

        # Computation of totals
        total_excl = total_incl = undiscounted_total_excl = undiscounted_total_incl = 0.0
        for values in values_per_grouping_key.values():
            total_excl += values["total_excluded_currency"]
            total_incl += values["total_excluded_currency"] + values["tax_amount_currency"]
        for grouping_key, values in values_undiscounted_per_grouping_key.items():
            if grouping_key:
                undiscounted_total_excl += values["total_excluded_currency"]
                undiscounted_total_incl += (
                    values["total_excluded_currency"] + values["tax_amount_currency"]
                )
        advantage_tax_excl = total_excl - undiscounted_total_excl
        advantage_tax_incl = total_incl - undiscounted_total_incl

        if not float_is_zero(advantage_tax_incl, precision_rounding=self.currency_id.rounding):
            return advantage_tax_excl, advantage_tax_incl

        return 0, 0

    @api.depends(
        "order_line.qty_delivered", "order_line.product_uom_qty", "state", "order_line.product_id"
    )
    def _compute_delivery_status(self):
        for order in self:
            lines_to_deliver = order.order_line.filtered(
                lambda line: line.product_id.type == "consu"
            )
            if order.state != "sale" or not lines_to_deliver:
                order.delivery_status = False
            elif all(
                line.qty_delivered >= line.product_uom_qty
                if line.product_uom_qty > 0
                else line.qty_delivered <= line.product_uom_qty
                for line in lines_to_deliver
            ):
                order.delivery_status = "full"
            elif any(line.qty_delivered for line in lines_to_deliver):
                order.delivery_status = "partial"
            else:
                order.delivery_status = "pending"

    @api.depends("client_order_ref", "origin", "partner_id")
    def _compute_duplicated_order_ids(self):
        draft_orders = self.filtered(lambda o: o.state == "draft")
        order_to_duplicate_orders = draft_orders._fetch_duplicate_orders()
        for order in draft_orders:
            order.duplicated_order_ids = [Command.set(order_to_duplicate_orders.get(order.id, []))]
        (self - draft_orders).duplicated_order_ids = False

    def _fetch_duplicate_orders(self):
        """Fetch duplicated orders.

        :return: Dictionary mapping order to its related duplicated orders.
        :rtype: dict
        """
        orders = self.filtered(lambda order: order.id and order.client_order_ref)
        if not orders:
            return {}

        self.env["sale.order"].flush_model([
            "company_id",
            "partner_id",
            "client_order_ref",
            "origin",
            "state",
        ])

        result = self.env.execute_query(
            SQL(
                """
                SELECT sale_order.id AS order_id,
                       array_agg(duplicate_order.id) AS duplicate_ids
                  FROM sale_order
                  JOIN sale_order AS duplicate_order
                    ON sale_order.company_id = duplicate_order.company_id
                   AND sale_order.id != duplicate_order.id
                   AND duplicate_order.state != 'cancel'
                   AND sale_order.partner_id = duplicate_order.partner_id
                   AND (
                        sale_order.origin = duplicate_order.name
                        OR sale_order.client_order_ref = duplicate_order.client_order_ref
                       )
                 WHERE sale_order.id IN %(orders)s
              GROUP BY sale_order.id
            """,
                orders=tuple(orders.ids),
            )
        )
        return {order_id: set(duplicate_ids) for order_id, duplicate_ids in result}

    @api.depends("order_line.customer_lead", "date_order", "state")
    def _compute_expected_date(self):
        """For service and combo (non-goods) products, we avoid computing the expected date.

        This method is extended in sale_stock to take the picking_policy of SO into account.
        """
        self.mapped("order_line")  # Prefetch indication
        for order in self:
            if order.state == "cancel":
                order.expected_date = False
                continue
            dates_list = order.order_line.filtered(
                lambda line: (
                    (not line.product_id or line.product_id.type == "consu")
                    and not line.display_type
                    and not line._is_delivery()
                )
            ).mapped(lambda line: line and line._expected_date())
            if dates_list:
                order.expected_date = order._select_expected_date(dates_list)
            else:
                order.expected_date = False

    def _select_expected_date(self, expected_dates):
        self.ensure_one()
        return min(expected_dates)

    @api.model
    def fields_get(self, allfields=None, attributes=None):
        descriptions = super().fields_get(allfields, attributes)
        for field_name in ("validity_date", "is_expired", "amount_untaxed"):
            description = descriptions.get(field_name, {})
            for attribute in ("searchable", "sortable", "groupable", "exportable"):
                if attribute in description:
                    description[attribute] = False
        return descriptions

    def _compute_is_expired(self):
        self.is_expired = False

    def _search_is_expired(self, operator, value):
        if operator not in ("in", "not in"):
            return NotImplemented
        return Domain.TRUE if (False in value) == (operator == "in") else Domain.FALSE

    def _search_is_unfulfilled(self, operator, value):  # noqa: PLR6301
        if operator not in {"=", "!="}:
            return NotImplemented

        if (operator == "=" and value) or (operator == "!=" and not value):
            effective_operator = "any"
        else:
            effective_operator = "not any"

        line_domain = Domain.custom(
            to_sql=lambda table: SQL("%s < %s", table.qty_delivered, table.product_uom_qty)
        ) & Domain([("product_id.product_tmpl_id.type", "!=", "service")])
        return Domain("state", "=", "sale") & Domain("order_line", effective_operator, line_domain)

    def _compute_show_ship_button(self):
        self.show_ship_button = False

    @api.depends("company_id", "fiscal_position_id")
    def _compute_tax_country_id(self):
        for record in self:
            if record.fiscal_position_id.foreign_vat:
                record.tax_country_id = record.fiscal_position_id.country_id
            else:
                record.tax_country_id = record.company_id.account_fiscal_country_id

    @api.depends("order_line.amount_to_invoice")
    def _compute_amount_to_invoice(self):
        for order in self:
            order.amount_to_invoice = sum(order.order_line.mapped("amount_to_invoice"))

    @api.depends("order_line.amount_invoiced")
    def _compute_amount_invoiced(self):
        for order in self:
            order.amount_invoiced = sum(order.order_line.mapped("amount_invoiced"))

    @api.depends("company_id", "partner_id", "amount_total")
    def _compute_partner_credit_warning(self):
        for order in self:
            order = order.with_company(order.company_id)
            order.partner_credit_warning = ""
            show_warning = (
                order.state in ("draft", "sent") and order.company_id.account_use_credit_limit
            )
            if show_warning:
                order.partner_credit_warning = self.env[
                    "account.move"
                ]._build_credit_warning_message(
                    order.sudo(),  # ensure access to `credit` & `credit_limit` fields
                    current_amount=(order.amount_total / order.currency_rate),
                )

    @api.depends_context("lang")
    @api.depends("order_line.price_subtotal", "currency_id", "company_id", "payment_term_id")
    def _compute_tax_totals(self):
        AccountTax = self.env["account.tax"]
        for order in self:
            order_lines = order._get_priced_lines()
            base_lines = [line._prepare_base_line_for_taxes_computation() for line in order_lines]
            base_lines += order._add_base_lines_for_early_payment_discount()
            AccountTax._add_tax_details_in_base_lines(base_lines, order.company_id)
            AccountTax._round_base_lines_tax_details(base_lines, order.company_id)
            order.tax_totals = AccountTax._get_tax_totals_summary(
                base_lines=base_lines,
                currency=order.currency_id or order.company_id.currency_id,
                company=order.company_id,
            )

    @api.depends(
        "order_line", "order_line.discount", "order_line.price_unit", "order_line.product_uom_qty"
    )
    def _compute_extra_total_fields(self):
        for order in self:
            basic_group = {"sequence": 1, "name": "basic", "lines": []}
            _excl, incl = order._get_advantages()
            if incl:
                basic_group["lines"].append({
                    "label": self.env._("Total Advantage"),
                    "value": abs(incl),
                })

            order.extra_total_fields = [basic_group]

    @api.depends("state")
    @api.depends_context("lang")
    def _compute_type_name(self):
        for record in self:
            if record.state in ("draft", "sent", "cancel"):
                record.type_name = record.env._("Quotation")
            else:
                record.type_name = record.env._("Sales Order")

    # portal.mixin override
    def _compute_access_url(self):
        super()._compute_access_url()
        for order in self:
            order.access_url = f"/my/orders/{order.id}"

    @api.depends("partner_id.name", "partner_id.sale_warn_msg", "order_line.sale_line_warn_msg")
    def _compute_sale_warning_text(self):
        if not self.env.user.has_group("sale.group_warning_sale"):
            self.sale_warning_text = ""
            return
        for order in self:
            warnings = OrderedSet()
            if partner_msg := order.partner_id.sale_warn_msg:
                warnings.add(
                    (order.partner_id.name or order.partner_id.display_name) + " - " + partner_msg
                )
            if partner_parent_msg := order.partner_id.parent_id.sale_warn_msg:
                parent = order.partner_id.parent_id
                warnings.add((parent.name or parent.display_name) + " - " + partner_parent_msg)
            for line in order.order_line:
                if product_msg := line.sale_line_warn_msg:
                    warnings.add(line.product_id.display_name + " - " + product_msg)
            order.sale_warning_text = "\n".join(warnings)

    @api.depends("commitment_date", "expected_date")
    def _compute_delivery_date(self):
        for order in self:
            order.delivery_date = order.commitment_date or order.expected_date

    @api.depends("order_line.qty_overage")
    def _compute_has_overages(self):
        for order in self:
            order.has_overages = any(line.qty_overage for line in order.order_line)

    @api.depends("company_id")
    def _compute_document_tax_mode(self):
        for order in self:
            if not order.document_tax_mode:
                company = order.company_id or self.env.company
                order.document_tax_mode = company.account_price_include

    # === CONSTRAINT METHODS ===#

    @api.constrains("company_id", "order_line")
    def _check_order_line_company_id(self):
        for order in self:
            invalid_companies = order.order_line.product_id.company_id.filtered(
                lambda c: order.company_id not in c._accessible_branches()
            )
            if invalid_companies:
                bad_products = order.order_line.product_id.filtered(
                    lambda p: p.company_id and p.company_id in invalid_companies
                )
                raise ValidationError(
                    order.env._(
                        "Your quotation contains products from company %(product_company)s whereas"
                        " your quotation belongs to company %(quote_company)s. \nPlease change the"
                        " company of your quotation or remove the products from other companies"
                        "(%(bad_products)s).",
                        product_company=", ".join(invalid_companies.sudo().mapped("display_name")),
                        quote_company=order.company_id.display_name,
                        bad_products=", ".join(bad_products.mapped("display_name")),
                    )
                )

    @api.constrains("prepayment_percent")
    def _check_prepayment_percent(self):
        for order in self:
            if not (0 <= order.prepayment_percent <= 1.0):
                raise ValidationError(
                    order.env._("Prepayment percentage must be a valid percentage.")
                )

    # === ONCHANGE METHODS ===#

    def onchange(self, values, field_names, fields_spec):
        self_with_context = self
        if not field_names:
            self_with_context = self.with_context(
                # Some warnings should not be displayed for the first onchange
                sale_onchange_first_call=True,
                # invoice & delivery address with higher `customer_rank` should take priority
                res_partner_search_mode="customer",
            )
        return super(SaleOrder, self_with_context).onchange(values, field_names, fields_spec)

    @api.onchange("commitment_date", "expected_date")
    def _onchange_commitment_date(self):
        """Warn if the commitment dates is sooner than the expected date."""
        if (
            self.commitment_date
            and self.expected_date
            and self.commitment_date < self.expected_date
        ):
            return {
                "warning": {
                    "title": self.env._("Requested date is too soon."),
                    "message": self.env._(
                        "The delivery date is sooner than the expected date."
                        " You may be unable to honor the delivery date."
                    ),
                }
            }

    @api.onchange("company_id")
    def _onchange_company_id_warning(self):
        if self.env.context.get("sale_onchange_first_call"):
            return None
        if self.order_line and self.state == "draft":
            return {
                "warning": {
                    "title": self.env._("Warning for the change of your quotation's company"),
                    "message": self.env._(
                        "Changing the company of an existing quotation might need some "
                        "manual adjustments in the details of the lines. You might "
                        "consider updating the prices."
                    ),
                }
            }

    @api.onchange("company_id")
    def _onchange_company_id(self):
        for order in self:
            # This can't be caught by a python constraint as it is only triggered at save
            # and a compute method needs this data to be set correctly before saving
            if not order.company_id:
                raise ValidationError(
                    order.env._(
                        "The company is required, please select one before making any other changes"
                        " to the sale order."
                    )
                )

    @api.onchange("fiscal_position_id")
    def _onchange_fpos_id_recompute_taxes(self):
        if self.order_line:
            self._recompute_taxes()

    @api.onchange("pricelist_id")
    def _onchange_pricelist_id_recompute_prices(self):
        # DO NOT ADD the `pricelist_id` as dependency to the order lines compute methods as it
        # would trigger unwanted recomputations as the orm recomputes all depending fields
        # regardless of whether the field was effectively modified.
        if self.order_line and not self.env.context.get("sale_onchange_first_call"):
            self._recompute_prices()

    @api.onchange("prepayment_amount")
    def _onchange_prepayment_amount(self):
        if self.amount_total:
            self.prepayment_percent = self.prepayment_amount / self.amount_total

    @api.onchange("order_line")
    def _onchange_order_line(self):
        linked_lines_by_line = self.order_line._get_linked_lines_by_line()
        for line in self.order_line:
            if line.display_type == "line_subsection" and not line.parent_id:
                line.display_type = "line_section"
            combo_item_lines = linked_lines_by_line[line].filtered("combo_item_id")
            if line.product_template_id.type != "combo":
                if combo_item_lines:
                    # Delete any linked combo item lines if the line's product is no longer a combo
                    # product.
                    self.order_line = [
                        Command.delete(linked_line.id) for linked_line in combo_item_lines
                    ]
            elif line.selected_combo_items:
                selected_combo_items = json.loads(line.selected_combo_items)
                if selected_combo_items and len(selected_combo_items) != len(
                    line.product_template_id.sudo().combo_ids
                ):
                    raise ValidationError(
                        line.env._(
                            "The number of selected combo items must match the number of available"
                            " combo choices."
                        )
                    )

                # Delete any existing combo item lines.
                delete_commands = [
                    Command.delete(linked_line.id) for linked_line in combo_item_lines
                ]
                # Create a new combo item line for each selected combo item.
                create_commands = [
                    Command.create({
                        "product_id": combo_item["product_id"],
                        "product_uom_qty": line.product_uom_qty,
                        "combo_item_id": combo_item["combo_item_id"],
                        "product_no_variant_attribute_value_ids": [
                            Command.set(combo_item["no_variant_attribute_value_ids"])
                        ],
                        "product_custom_attribute_value_ids": [Command.clear()]
                        + [
                            Command.create(attribute_value)
                            for attribute_value in combo_item["product_custom_attribute_values"]
                        ],
                        # Combo item lines should come directly after their combo product line.
                        "sequence": line.sequence + item_index + 1,
                        # If the linked line exists in DB, populate linked_line_id, otherwise
                        # populate linked_virtual_id.
                        "linked_line_id": line.id if line._origin else False,
                        "linked_virtual_id": line.virtual_id if not line._origin else False,
                    })
                    for item_index, combo_item in enumerate(selected_combo_items)
                ]
                # Shift any lines coming after the combo product line so that the combo item lines
                # come first.
                update_commands = [
                    Command.update(
                        order_line.id, {"sequence": order_line.sequence + len(selected_combo_items)}
                    )
                    for order_line in self.order_line
                    if order_line.sequence > line.sequence
                ]

                # Clear `selected_combo_items` to avoid applying the same changes multiple times.
                line.selected_combo_items = False
                self.order_line = delete_commands + create_commands + update_commands
            elif (
                combo_item_lines
                # Only update the combo item lines if the line's combo choices haven't changed.
                and combo_item_lines.combo_item_id.combo_id == line.product_template_id.combo_ids
            ):
                lines_to_sync = combo_item_lines.filtered(
                    lambda cil: (
                        cil.product_uom_qty != line.product_uom_qty or cil.discount != line.discount
                    )
                )
                if lines_to_sync:
                    lines_to_sync.update({
                        "product_uom_qty": line.product_uom_qty,
                        "discount": line.discount,
                    })

    # === CRUD METHODS ===#

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('state') == 'sent':
                vals.setdefault('quotation_sent_at', fields.Datetime.now())
                vals.setdefault('quotation_first_sent_at', vals['quotation_sent_at'])
            if vals.get('quotation_result') in ('won', 'lost'):
                vals.setdefault('quotation_feedback_date', fields.Date.context_today(self))
            if vals.get("name", self.env._("New")) == self.env._("New"):
                seq_date = (
                    fields.Datetime.context_timestamp(
                        self, fields.Datetime.to_datetime(vals["date_order"])
                    )
                    if "date_order" in vals
                    else None
                )
                vals["name"] = self.env["ir.sequence"].with_company(
                    vals.get("company_id")
                ).next_by_code("sale.order", sequence_date=seq_date) or self.env._("New")

        return super().create(vals_list)

    def _get_copiable_order_lines(self):
        """Return the order lines that can be copied to a new order."""
        return self.order_line.filtered(lambda sol: not sol.is_downpayment)

    def copy_data(self, default=None):
        default = dict(default or {})
        default_has_no_order_line = "order_line" not in default
        default.setdefault("order_line", [])
        vals_list = super().copy_data(default=default)
        if default_has_no_order_line:
            for order, vals in zip(self, vals_list):
                vals["order_line"] = [
                    Command.create(line_vals)
                    for line_vals in order._get_copiable_order_lines().copy_data()
                ]
        return vals_list

    @api.ondelete(at_uninstall=False)
    def _unlink_except_draft_or_cancel(self):
        for order in self:
            if order.state not in ("draft", "cancel"):
                raise UserError(
                    order.env._(
                        "You can not delete a sent quotation or a confirmed sales order."
                        " You must first cancel it."
                    )
                )

    def write(self, vals):
        vals = dict(vals)
        if "pricelist_id" in vals and any(so.state == "sale" for so in self):
            raise UserError(self.env._("You cannot change the pricelist of a confirmed order !"))
        if vals.get('quotation_result') in ('won', 'lost'):
            vals.setdefault('quotation_feedback_date', fields.Date.context_today(self))
            if vals['quotation_result'] == 'lost':
                vals.setdefault('quotation_lost_reason', 'unrecorded')
        elif vals.get('quotation_result') == 'pending':
            vals.update(quotation_feedback_date=False, quotation_lost_reason=False)
        if vals.get('quotation_result') == 'won':
            vals['quotation_lost_reason'] = False
        if vals.get("quotation_workflow_step") == "contract":
            vals["quotation_contract_at"] = vals.get("signed_on") or fields.Datetime.now()
        if vals.get('state') == 'sent':
            for order in self:
                sent_vals = dict(vals)
                if order.state != 'sent':
                    sent_vals.setdefault('quotation_sent_at', fields.Datetime.now())
                if not order.quotation_first_sent_at:
                    sent_vals.setdefault('quotation_first_sent_at', sent_vals.get('quotation_sent_at') or fields.Datetime.now())
                super(SaleOrder, order).write(sent_vals)
            return True
        return super().write(vals)

    # === ACTION METHODS ===#

    @api.readonly
    def action_open_discount_wizard(self):
        self.ensure_one()
        return {
            "name": self.env._("Discount"),
            "type": "ir.actions.act_window",
            "res_model": "sale.order.discount",
            "views": [(False, "form")],
            "view_mode": "form",
            "target": "new",
        }

    @api.depends("state", "quotation_workflow_step")
    def _compute_quotation_status(self):
        for order in self:
            step = order.quotation_workflow_step
            if order.state in ("draft", "sent") and step == "contract":
                order.quotation_status = "contract"
            elif order.state == "draft" and step == "quotation":
                order.quotation_status = "quotation"
            else:
                order.quotation_status = order.state

    def action_mark_quotation(self):
        if any(order.quotation_status != "draft" for order in self):
            raise UserError("当前单据应处于草稿状态。")
        if any(line.selection_pricing_result and line.saved_result_state != "calculated" for line in self.order_line):
            raise UserError("当前规格或折扣已修改，请先重新计算报价。")
        return self.write({
            "quotation_workflow_step": "quotation",
            "quotation_prepared_at": fields.Datetime.now(),
        })

    def action_mark_quotation_contract(self):
        if any(order.quotation_status not in ("quotation", "sent") for order in self):
            raise UserError("请先把草稿转为报价单，再标记为合同。")
        return self.write({"quotation_workflow_step": "contract"})

    def action_unmark_quotation_contract(self):
        if any(order.quotation_status != "contract" for order in self):
            raise UserError("当前单据应处于合同状态。")
        return self.write({"quotation_workflow_step": "quotation", "quotation_contract_at": False})

    def action_draft(self):
        orders = self.filtered(lambda s: s.state in ["cancel", "sent"])
        return orders.write({
            "state": "draft",
            "quotation_workflow_step": False,
            "quotation_prepared_at": False,
            "quotation_sent_at": False,
            "quotation_contract_at": False,
            "quotation_confirmed_at": False,
            "signature": False,
            "signed_by": False,
            "signed_on": False,
        })

    def action_quotation_send(self):
        """Open a wizard to compose an email, with relevant mail template loaded by default."""
        self.filtered(
            lambda so: so.state in ("draft", "sent")
        ).order_line._validate_analytic_distribution()

        ctx = {
            "default_model": "sale.order",
            "default_res_ids": self.ids,
            "default_composition_mode": "comment",
            "email_notification_allow_footer": True,
            "hide_mail_template_management_options": True,
            "proforma": self.env.context.get("proforma", False),
        }

        if len(self) > 1:
            ctx["default_composition_mode"] = "mass_mail"
        else:
            ctx.update({"force_email": True})
            if not self.env.context.get("hide_default_template"):
                mail_template = self._find_mail_template()
                if mail_template:
                    ctx.update({"default_template_id": mail_template.id, "mark_so_as_sent": True})
            else:
                for order in self:
                    order._portal_ensure_token()

        action = {
            "name": self.env._("Send"),
            "type": "ir.actions.act_window",
            "view_mode": "form",
            "res_model": "mail.compose.message",
            "views": [(False, "form")],
            "view_id": False,
            "target": "new",
            "context": ctx,
        }
        if (
            self.env.context.get("check_document_layout")
            and not self.env.context.get("discard_logo_check")
            and self.env.is_admin()
            and not self.env.company.external_report_layout_id
        ):
            layout_action = self.env["ir.actions.report"]._action_configure_external_report_layout(
                action
            )
            # Need to remove this context for windows action
            action.pop("close_on_report_download", None)
            layout_action["context"]["dialog_size"] = "extra-large"
            return layout_action
        return action

    def _find_mail_template(self):
        """Get the appropriate mail template for the current sales order based on its state.

        If the SO is confirmed, we return the mail template for the sale confirmation.
        Otherwise, we return the quotation email template.

        :return: The correct mail template based on the current status
        :rtype: record of `mail.template` or `None` if not found
        """
        self.ensure_one()
        if self.env.context.get("proforma"):
            return self.env.ref("sale.email_template_proforma", raise_if_not_found=False)
        if self.state != "sale":
            return self.env.ref("sale.email_template_edi_sale", raise_if_not_found=False)
        return self._get_confirmation_template()

    def _get_confirmation_template(self):
        """Get the mail template sent on SO confirmation (or for confirmed SO's).

        :return: `mail.template` record or None if default template wasn't found
        """
        self.ensure_one()
        default_confirmation_template_id = (
            self.env["ir.config_parameter"].sudo().get_int("sale.default_confirmation_template")
        )
        default_confirmation_template = (
            self.env["mail.template"].browse(default_confirmation_template_id).exists()
        )
        if default_confirmation_template:
            return default_confirmation_template
        return self.env.ref("sale.mail_template_sale_confirmation", raise_if_not_found=False)

    def action_quotation_sent(self):
        """Mark the given draft quotation(s) as sent.

        :raise: UserError if any given SO is not in draft state.
        """
        if any(order.state != "draft" for order in self):
            raise UserError(self.env._("Only draft orders can be marked as sent directly."))

        self.write({"state": "sent"})

    def action_confirm(self):
        """Confirm the given quotation(s) and set their confirmation dates.

        If the corresponding setting is enabled, also locks the Sale Order.

        :return: True
        :rtype: bool
        :raise: UserError if trying to confirm cancelled SO's
        """
        for order in self:
            error_msg = order._confirmation_error_message()
            if error_msg:
                raise UserError(error_msg)

        self.order_line._validate_analytic_distribution()

        self.write(self._prepare_confirmation_values())

        # Context key 'default_name' is sometimes propagated up to here.
        # We don't need it and it creates issues in the creation of linked records.
        context = self.env.context.copy()
        context.pop("default_name", None)
        context.pop("default_user_id", None)

        self.with_context(context)._action_confirm()
        self.filtered(lambda so: so._should_be_locked()).action_lock()

        if self.env.context.get("send_email"):
            self._send_order_confirmation_mail()

        return True

    def _should_be_locked(self):
        self.ensure_one()
        # Public user can confirm SO, so we check the group on any record creator.
        return self.env["res.groups"]._is_feature_enabled("sale.group_auto_done_setting")

    def _confirmation_error_message(self):
        """Return whether order can be confirmed or not if not then return error message."""
        self.ensure_one()
        if self.state not in {"draft", "sent"}:
            return self.env._("Some orders are not in a state requiring confirmation.")
        if self.env.context.get("require_quotation_contract") and self.quotation_status != "contract":
            return "请在客户确认报价后，先将单据标记为合同。"

        return False

    def _prepare_confirmation_values(self):
        """Prepare the sales order confirmation values.

        Note: self can contain multiple records.

        :return: Sales Order confirmation values
        :rtype: dict
        """
        confirmed_at = fields.Datetime.now()
        return {"state": "sale", "date_order": confirmed_at, "quotation_confirmed_at": confirmed_at}

    def _action_confirm(self):
        """Provide a hook method between confirmation and lock/confirmation mail generation."""

    def _send_order_confirmation_mail(self):
        """Send a mail to the SO customer to inform them that their order has been confirmed.

        :return: None
        """
        for order in self:
            mail_template = order._get_confirmation_template()
            order._send_order_notification_mail(mail_template)

    def _send_payment_succeeded_for_order_mail(self):
        """Send a mail to the SO customer to inform them that a payment has been initiated.

        :return: None
        """
        mail_template = self.env.ref(
            "sale.mail_template_sale_payment_executed", raise_if_not_found=False
        )
        for order in self:
            order._send_order_notification_mail(mail_template)

    def _send_order_notification_mail(self, mail_template, allow_deferred_sending=True):
        """Send a mail to the customer.

        If the `sale.async_emails` ICP is set and `allow_deferred_sending` is true, order status
        emails are sent asynchronously through a cron.

        Note: self.ensure_one()

        :param mail.template mail_template: the template used to generate the mail
        :param bool allow_deferred_sending: Whether the email can be sent asynchronously.
        :return: None
        """
        self.ensure_one()

        if not mail_template:
            return

        if self.env.su:
            # sending mail in sudo was meant for it being sent from superuser
            self = self.with_user(SUPERUSER_ID)

        async_send = self.env["ir.config_parameter"].sudo().get_bool("sale.async_emails")
        cron = self.env.ref("sale.send_pending_emails_cron", raise_if_not_found=False)
        cron_enabled = cron and cron.sudo().active
        if async_send and cron_enabled and allow_deferred_sending:
            # Schedule the email to be sent asynchronously.
            self.pending_email_template_id = mail_template
            cron._trigger()
        else:  # Async emails are disabled, either by the user or we are in the cron job.
            # Send the email synchronously.
            self.with_context(force_send=True).message_post_with_source(
                mail_template,
                email_layout_xmlid="mail.mail_notification_light",
                subtype_xmlid="mail.mt_comment",
            )

    def _validate_order(self):
        """Confirm the sale order and send a confirmation email.

        :return: None
        """
        self.with_context(send_email=True).action_confirm()

    @api.model
    def _cron_send_pending_emails(self):
        """Find and send pending order status emails asynchronously.

        :return: None
        """
        pending_email_orders = self.search([("pending_email_template_id", "!=", False)])
        self.env["ir.cron"]._commit_progress(remaining=len(pending_email_orders))
        for order in pending_email_orders:
            order = (
                order.with_prefetch()
            )  # Avoid pre-fetching after each cache invalidation due to committing.
            order._send_order_notification_mail(
                order.pending_email_template_id, allow_deferred_sending=False
            )  # Resume the email sending.
            order.pending_email_template_id = None
            remaining_time = self.env["ir.cron"]._commit_progress(processed=1)
            if not remaining_time:
                break

    def action_close_invoicing(self):
        """Mark sales orders as manually closed for invoicing."""
        draft_orders = self.filtered(lambda o: o.state != "sale")
        if draft_orders:
            orders_name = ", ".join(draft_orders.mapped("name"))
            raise UserError(
                self.env._(
                    "Cannot close %(orders_name)s: must be in 'Sales Order' state.",
                    orders_name=orders_name,
                )
            )
        orders_to_close = self.filtered(lambda o: not o.invoicing_closed)
        self.invoicing_closed = True
        orders_to_close._message_log_batch(
            bodies={order.id: self.env._("Invoicing closed") for order in orders_to_close}
        )

    def action_reopen_order(self):
        """Reopen invoicing for manually closed sales orders."""
        orders_to_reopen = self.filtered("invoicing_closed")
        orders_to_reopen.invoicing_closed = False
        orders_to_reopen._message_log_batch(
            bodies={order.id: self.env._("Invoicing reopened") for order in orders_to_reopen}
        )

    def action_lock(self):
        self.locked = True

    def action_unlock(self):
        self.locked = False

    def action_cancel(self):
        """Cancel sales order and related draft invoices."""
        if any(order.locked for order in self):
            raise UserError(self.env._("You cannot cancel a locked order. Please unlock it first."))
        return self._action_cancel()

    def _action_cancel(self):
        inv = self.invoice_ids.filtered(lambda inv: inv.state == "draft")
        inv.button_cancel()
        return self.write({"state": "cancel"})

    @api.readonly
    def action_preview_sale_order(self):
        self.ensure_one()
        url = urljoin(self.get_base_url(), self.get_portal_url())
        return {"type": "ir.actions.act_url", "target": "new", "url": url}

    def _recompute_taxes(self):
        lines_to_recompute = self.order_line.filtered(lambda line: not line.display_type)

        lines_to_recompute.with_context(recompute_unit_price_on_tax_change=True)._compute_tax_ids()

    def _recompute_prices(self):
        lines_to_recompute = self._get_update_prices_lines()
        lines_to_recompute.invalidate_recordset(["pricelist_item_id"])
        lines_to_recompute.with_context(force_price_recomputation=True)._compute_price_unit()
        # Special case: we want to overwrite the existing discount on _recompute_prices call
        # i.e. to make sure the discount is correctly reset
        # if pricelist rule is different than when the price was first computed.
        lines_to_recompute.discount = 0.0
        lines_to_recompute._compute_discount()

    @api.readonly
    def action_open_business_doc(self):
        self.ensure_one()
        return {
            "name": self.env._("Order"),
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "res_id": self.id,
            "views": [(False, "form")],
        }

    # INVOICING #

    def _prepare_invoice(self):
        """
        Prepare the dict of values to create the new invoice for a sales order. This method may be
        overridden to implement custom invoice generation (making sure to call super() to establish
        a clean extension chain).
        """
        self.ensure_one()

        txs_to_be_linked = self.sudo().transaction_ids.filtered(
            lambda tx: (
                tx.state in ("pending", "authorized")
                or (
                    tx.state == "done"
                    and tx.payment_id.move_id.state == "posted"
                    and not tx.payment_id.is_reconciled
                )
            )
        )

        values = {
            "ref": self.client_order_ref or self.name,
            "move_type": "out_invoice",
            "narration": self.note,
            "currency_id": self.currency_id.id,
            "campaign_id": self.campaign_id.id,
            "medium_id": self.medium_id.id,
            "source_id": self.source_id.id,
            "utm_reference": f"{self.utm_reference._name},{self.utm_reference.id}"
            if self.utm_reference
            else False,
            "team_id": self.team_id.id,
            "partner_id": self.partner_invoice_id.id,
            "partner_shipping_id": self.partner_shipping_id.id,
            "fiscal_position_id": (
                self.fiscal_position_id
                or self.fiscal_position_id._get_fiscal_position(self.partner_invoice_id)
            ).id,
            "invoice_origin": self.name,
            "invoice_payment_term_id": self.payment_term_id.id,
            "preferred_payment_method_line_id": self.preferred_payment_method_line_id.id,
            "invoice_user_id": self.user_id.id,
            "payment_reference": self.reference,
            "transaction_ids": [Command.set(txs_to_be_linked.ids)],
            "company_id": self.company_id.id,
            "invoice_line_ids": [],
            "user_id": self.user_id.id,
            "invoice_incoterm_id": self.incoterm.id,
            "incoterm_location": self.incoterm_location,
            "document_tax_mode": self.document_tax_mode,
        }
        if self.journal_id:
            values["journal_id"] = self.journal_id.id
        return values

    @api.readonly
    def action_view_invoice(self, invoices=False):
        if not invoices:
            invoices = self.mapped("invoice_ids")
        action = self.env["ir.actions.actions"]._for_xml_id("account.action_move_out_invoice_type")
        if len(invoices) > 1:
            action["domain"] = [("id", "in", invoices.ids)]
        elif len(invoices) == 1:
            form_view = [(self.env.ref("account.view_move_form").id, "form")]
            if "views" in action:
                action["views"] = form_view + [
                    (state, view) for state, view in action["views"] if view != "form"
                ]
            else:
                action["views"] = form_view
            action["res_id"] = invoices.id
        else:
            action = {"type": "ir.actions.act_window_close"}

        context = {"default_move_type": "out_invoice"}
        if len(self) == 1:
            context.update({
                "default_partner_id": self.partner_id.id,
                "default_partner_shipping_id": self.partner_shipping_id.id,
                "default_invoice_payment_term_id": self.payment_term_id.id
                or self.partner_id.property_payment_term_id.id
                or self
                .env["account.move"]
                .default_get(["invoice_payment_term_id"])
                .get("invoice_payment_term_id"),
            })
        action["context"] = context
        return action

    def action_view_payment_transaction(self):
        action = self.env["ir.actions.act_window"]._for_xml_id("payment.action_payment_transaction")
        if len(self.transaction_ids) == 1:
            action["view_mode"] = "form"
            action["res_id"] = self.transaction_ids.id
            action["views"] = []
        else:
            action["domain"] = [("id", "in", self.transaction_ids.ids)]
        return action

    def _get_invoice_grouping_keys(self):
        return [
            "company_id",
            "partner_id",
            "partner_shipping_id",
            "currency_id",
            "fiscal_position_id",
            "invoice_incoterm_id",
            "incoterm_location",
        ]

    def _nothing_to_invoice_error_message(self):
        return self.env._(
            "Cannot create an invoice. No items are available to invoice.\n\n"
            "To resolve this issue, please ensure that:\n"
            "   \u2022 The products have been delivered before attempting to invoice them.\n"
            "   \u2022 The invoicing policy of the product is configured correctly.\n\n"
            "If you want to invoice based on ordered quantities instead:\n"
            "   \u2022 For consumable or storable products, open the product, go to the"
            " 'General Information' tab and change the 'Invoicing Policy' from 'Delivered"
            " Quantities' to 'Ordered Quantities'.\n"
            "   \u2022 For services (and other products), change the 'Invoicing Policy' to"
            " 'Prepaid/Fixed Price'.\n"
        )

    def _get_update_prices_lines(self):
        """Determine the lines whose prices should be recomputed on pricelist change."""
        return self.order_line.filtered(lambda line: not line.display_type)

    def _get_invoiceable_lines(self, final=False):
        """Return the invoiceable lines for order `self`."""
        down_payment_line_ids = []
        invoiceable_line_ids = []
        section_line_ids = []
        subsection_line_ids = []
        precision = self.env["decimal.precision"].precision_get("Product Unit")

        for line in self.order_line:
            if line.display_type == "line_section":
                section_line_ids = [line.id]  # Start a new section.
                subsection_line_ids = []
                continue
            if line.display_type == "line_subsection":
                subsection_line_ids = [line.id]  # Start a new subsection.
                continue
            if line.display_type != "line_note" and float_is_zero(
                line.qty_to_invoice, precision_digits=precision
            ):
                continue
            if (
                line.qty_to_invoice > 0
                or (line.qty_to_invoice < 0 and final)
                or line.display_type == "line_note"
            ):
                if line.is_downpayment:
                    # Keep down payment lines separately, to put them together
                    # at the end of the invoice, in a specific dedicated section.
                    down_payment_line_ids.append(line.id)
                    continue
                # If the invoicable line is under subsection
                if subsection_line_ids:
                    if line.display_type:
                        subsection_line_ids.append(line.id)
                        continue
                    # Extend the subsection lines too if at least one invoicable line is under
                    # subsection
                    invoiceable_line_ids.extend(section_line_ids + subsection_line_ids)
                    subsection_line_ids = []
                    section_line_ids = []
                # If the invoicable line is under section
                elif section_line_ids:
                    if line.display_type:
                        section_line_ids.append(line.id)
                        continue
                    invoiceable_line_ids.extend(section_line_ids)
                    section_line_ids = []
                    subsection_line_ids = []
                invoiceable_line_ids.append(line.id)

        return self.env["sale.order.line"].browse(invoiceable_line_ids + down_payment_line_ids)

    def _create_account_invoices(self, invoice_vals_list):
        """Small method to allow overriding the behavior right after an invoice is created."""
        # Manage the creation of invoices in sudo because a salesperson must be able to generate an
        # invoice from a sale order without "billing" access rights. However, he should not be able
        # to create an invoice from scratch.
        AccountMoveSudo = (
            self.env["account.move"].sudo().with_context(default_move_type="out_invoice")
        )
        moves = AccountMoveSudo.browse()
        for company_id, vals_list_for_company in groupby(
            invoice_vals_list, lambda vals: vals.get("company_id")
        ):
            moves |= AccountMoveSudo.with_company(company_id).create(list(vals_list_for_company))

        return moves

    def _create_invoices(self, final=False, grouped=False):
        """Create invoice(s) for the given Sales Order(s).

        :param bool final: if True, refunds will be generated if necessary
        :param bool grouped: if True, invoices are grouped by SO id.
            If False, invoices are grouped by keys returned by :meth:`_get_invoice_grouping_keys`
        :returns: created invoices
        :rtype: `account.move` recordset

        :raises: UserError if one of the orders has no invoiceable lines.
        """
        if not self.env["account.move"].has_access("create"):
            try:
                self.check_access("write")
            except AccessError:
                return self.env["account.move"]

        # 1) Create invoices.
        invoice_vals_list = []
        invoice_item_sequence = 0  # Incremental sequencing to keep the lines order on the invoice.
        for order in self:
            if order.partner_invoice_id.lang:
                order = order.with_context(lang=order.partner_invoice_id.lang)
            order = order.with_company(order.company_id)

            invoice_vals = order._prepare_invoice()
            invoiceable_lines = order._get_invoiceable_lines(final)

            if all(line.display_type for line in invoiceable_lines):
                continue

            invoice_line_vals = []
            down_payment_section_added = False
            for line in invoiceable_lines:
                if not down_payment_section_added and line.is_downpayment:
                    # Create a dedicated section for the down payments
                    # (put at the end of the invoiceable_lines)
                    invoice_line_vals.append(
                        Command.create(
                            order._prepare_down_payment_section_line(sequence=invoice_item_sequence)
                        )
                    )
                    down_payment_section_added = True
                    invoice_item_sequence += 1

                optional_values = {"sequence": invoice_item_sequence}

                # When creating the final invoice, we want to express the lines representing
                # the full order but negate the already created down payment lines.
                # At this point, on the sale order, the down payment lines have a non-empty
                # 'extra_tax_data' containing a price unit greater than zero and a quantity of 0.0.
                if line.is_downpayment:
                    optional_values["quantity"] = -1.0
                    optional_values["extra_tax_data"] = self.env[
                        "account.tax"
                    ]._reverse_quantity_base_line_extra_tax_data(line.extra_tax_data)

                invoice_line_vals.extend(
                    Command.create(vals)
                    for vals in line._prepare_invoice_lines_vals_list(**optional_values)
                )
                invoice_item_sequence += 1

            invoice_vals["invoice_line_ids"] += invoice_line_vals
            invoice_vals_list.append(invoice_vals)

        if not invoice_vals_list and self.env.context.get("raise_if_nothing_to_invoice", True):
            raise UserError(self._nothing_to_invoice_error_message())

        # 2) Manage 'grouped' parameter: group by (partner_id, partner_shipping_id, currency_id).
        if not grouped:
            new_invoice_vals_list = []
            invoice_grouping_keys = self._get_invoice_grouping_keys()
            invoice_vals_list = sorted(
                invoice_vals_list,
                key=lambda x: [x.get(grouping_key) for grouping_key in invoice_grouping_keys],
            )
            for _grouping_keys, invoices in groupby(
                invoice_vals_list,
                key=lambda x: [x.get(grouping_key) for grouping_key in invoice_grouping_keys],
            ):
                origins = set()
                payment_refs = set()
                refs = set()
                ref_invoice_vals = None
                for invoice_vals in invoices:
                    if not ref_invoice_vals:
                        ref_invoice_vals = invoice_vals
                    else:
                        ref_invoice_vals["invoice_line_ids"] += invoice_vals["invoice_line_ids"]
                    origins.add(invoice_vals["invoice_origin"])
                    payment_refs.add(invoice_vals["payment_reference"])
                    refs.add(invoice_vals["ref"])
                ref_invoice_vals.update({
                    "ref": ", ".join(refs)[:2000],
                    "invoice_origin": ", ".join(origins),
                    "payment_reference": (len(payment_refs) == 1 and payment_refs.pop()) or False,
                })
                new_invoice_vals_list.append(ref_invoice_vals)
            invoice_vals_list = new_invoice_vals_list

        # 3) Create invoices.

        # As part of the invoice creation, we make sure the sequence of multiple SO do not interfere
        # in a single invoice. Example:
        # SO 1:
        # - Section A (sequence: 10)
        # - Product A (sequence: 11)
        # SO 2:
        # - Section B (sequence: 10)
        # - Product B (sequence: 11)
        #
        # If SO 1 & 2 are grouped in the same invoice, the result will be:
        # - Section A (sequence: 10)
        # - Section B (sequence: 10)
        # - Product A (sequence: 11)
        # - Product B (sequence: 11)
        #
        # Resequencing should be safe, however we resequence only if there are less invoices than
        # orders, meaning a grouping might have been done. This could also mean that only a part
        # of the selected SO are invoiceable, but resequencing in this case shouldn't be an issue.
        if len(invoice_vals_list) < len(self):
            SaleOrderLine = self.env["sale.order.line"]
            for invoice in invoice_vals_list:
                sequence = 1
                for line in invoice["invoice_line_ids"]:
                    line[2]["sequence"] = SaleOrderLine._get_invoice_line_sequence(
                        new=sequence, old=line[2]["sequence"]
                    )
                    sequence += 1

        moves = self._create_account_invoices(invoice_vals_list)

        # 4) Some moves might actually be refunds: convert them if the total amount is negative
        # We do this after the moves have been created since we need taxes, etc. to know if the
        # total is actually negative or not
        if final and (moves_to_switch := moves.sudo().filtered(lambda m: m.amount_total < 0)):
            with self.env.protecting([moves._fields["team_id"]], moves_to_switch):
                moves_to_switch.action_switch_move_type()
                self.invoice_ids._set_reversed_entry(moves_to_switch)

        moves._link_analytic_lines_to_invoice()

        for move in moves:
            move.message_post_with_source(
                "mail.message_origin_link",
                render_values={"self": move, "origin": move.line_ids.sale_line_ids.order_id},
                subtype_xmlid="mail.mt_note",
            )
        return moves

    # MAIL #

    def _discard_tracking(self):
        self.ensure_one()
        return self.state == "draft" and request and request.env.context.get("catalog_update")

    def _track_finalize(self):
        """Override of `mail` to prevent logging changes when the SO is in a draft state."""
        if (
            len(self) == 1
            # The method _track_finalize is sometimes called too early or too late and it
            # might cause a desynchronization with the cache, thus this condition is needed.
            and self.id in self._fields["state"]._get_cache(self.env)
            and self._discard_tracking()
        ):
            self._track_clear()
        return super()._track_finalize()

    def message_post(self, **kwargs):
        if self.env.context.get("mark_so_as_sent"):
            self.filtered(lambda o: o.state == "draft").with_context(tracking_disable=True).write({
                "state": "sent"
            })
            kwargs["notify_author_mention"] = kwargs.get("notify_author_mention", True)
        return super().message_post(**kwargs)

    def _notify_get_recipients_groups(self, message, model_description):
        # Give access button to users and portal customer as portal is integrated
        # in sale. Customer and portal group have probably no right to see
        # the document so they don't have the access button.
        groups = super()._notify_get_recipients_groups(message, model_description)
        if not self:
            return groups

        self.ensure_one()
        if self.env.context.get("proforma"):
            for group in [
                g for g in groups if g[0] in ("portal_customer", "portal", "follower", "customer")
            ]:
                group[2]["has_button_access"] = False
            return groups

        # portal customers have full access (existence not granted, depending on partner_id)
        try:
            customer_portal_group = next(group for group in groups if group[0] == "portal_customer")
        except StopIteration:
            pass
        else:
            access_opt = customer_portal_group[2].setdefault("button_access", {})
            if self.state in ("draft", "sent"):
                access_opt["title"] = self.env._("View Quotation")

        return groups

    def _notify_by_email_prepare_rendering_context(
        self,
        message,
        model_description=False,
        force_email_company=False,
        force_email_lang=False,
        force_record_name=False,
        force_header=False,
        force_footer=False,
    ):
        render_context = super()._notify_by_email_prepare_rendering_context(
            message,
            model_description=model_description,
            force_email_company=force_email_company,
            force_email_lang=force_email_lang,
            force_record_name=force_record_name,
            force_header=force_header,
            force_footer=force_footer,
        )
        lang_code = render_context.get("lang")
        record = render_context["record"]
        subtitles = [
            f"{record.name} - {record.partner_id.name}" if record.partner_id.name else record.name
        ]
        if self.amount_total:
            # Do not show the price in subtitles if zero (e.g. e-commerce orders are created empty)
            subtitles.append(
                format_amount(self.env, self.amount_total, self.currency_id, lang_code=lang_code)
            )

        render_context["subtitles"] = subtitles
        return render_context

    def _phone_get_number_fields(self):
        """No phone or mobile field is available on sale model. Instead SMS will
        fallback on partner-based computation using ``_mail_get_partner_fields``."""
        return []

    def _track_log_get_default_subtype(self, track_init_values):
        self.ensure_one()
        if "state" in track_init_values and self.state == "sale":
            return self.env.ref("sale.mt_order_confirmed")
        if "state" in track_init_values and self.state == "sent":
            return self.env.ref("sale.mt_order_sent")
        return super()._track_log_get_default_subtype(track_init_values)

    def _get_model_description(self, model_name):
        if not self:
            return super()._get_model_description(model_name)
        return self.type_name

    # PAYMENT #

    def _force_lines_to_invoice_policy_order(self):
        """Force the qty_to_invoice to be computed as if the invoice_policy
        was set to "Ordered quantities", independently of the product configuration.

        This is needed for the automatic invoice logic, as we want to automatically
        invoice the full SO when it's paid.
        """
        for line in self.order_line:
            if line.state == "sale":
                # No need to set 0 as it is already the standard logic in the compute method.
                line.qty_to_invoice = line.product_uom_qty - line.qty_invoiced

    def payment_action_capture(self):
        """Capture all transactions linked to this sale order."""
        self.ensure_one()
        payment_utils.check_rights_on_recordset(self)

        # In sudo mode to bypass the checks on the rights on the transactions.
        return self.sudo().transaction_ids.action_capture()

    def payment_action_void(self):
        """Void all transactions linked to this sale order."""
        payment_utils.check_rights_on_recordset(self)

        # In sudo mode to bypass the checks on the rights on the transactions.
        self.sudo().authorized_transaction_ids.action_void()

    def get_portal_last_transaction(self):
        self.ensure_one()
        return self.sudo().transaction_ids._get_last()

    def _get_order_lines_to_report(self):
        down_payment_lines = self.order_line.filtered(
            lambda line: (
                line.is_downpayment and not line.display_type and not line._get_downpayment_state()
            )
        )

        def show_line(line):
            if line.is_downpayment:
                return (
                    # Only show the down payment section if down payments were posted
                    (line.display_type and down_payment_lines)
                    # Only show posted down payments
                    or line in down_payment_lines
                )
            return line.display_type == "line_section" or not (
                line.parent_id.collapse_composition or line.parent_id.parent_id.collapse_composition
            )

        return self.order_line.filtered(show_line)

    def _get_default_payment_link_values(self):
        """Override of `payment` to compute the default values of the payment link wizard."""
        self.ensure_one()

        prepayment_amount = self._get_prepayment_required_amount()
        if self.state in ("draft", "sent") and self.prepayment_percent > 0:
            # Suggest the amount needed to confirm the quote.
            suggested_amount = max(prepayment_amount - self.amount_paid, 0)
        else:  # The order is confirmed or doesn't require payment.
            suggested_amount = max(self.amount_total - self.amount_paid, 0.0)
        return {
            "currency_id": self.currency_id.id,
            "partner_id": self.partner_invoice_id.id,
            "amount": suggested_amount,
            "amount_paid": self.amount_paid,
            "prepayment_amount": prepayment_amount,
        }

    # EDI #

    def _get_edi_builders(self):
        return []

    def create_document_from_attachment(self, attachment_ids):
        """Create the sale orders from given attachment_ids and redirect newly create order view.

        :param list attachment_ids: List of attachments process.
        :return: An action redirecting to related sale order view.
        :rtype: dict
        """
        attachments = self.env["ir.attachment"].browse(attachment_ids)
        if not attachments:
            raise UserError(self.env._("No attachment was provided"))

        orders = self.with_context(
            default_partner_id=self.env.user.partner_id.id
        )._create_records_from_attachments(attachments)

        return orders._get_records_action(name=self.env._("Generated Orders"))

    # PORTAL #

    def _has_to_be_signed(self):
        """Determine whether a sale order has to be signed.

        A sale order has to be signed when:
        - its state is 'draft' or `sent`
        - it's not expired;
        - it requires a signature;
        - it's not already signed.

        Note: self.ensure_one()

        :return: Whether the sale order has to be signed.
        :rtype: bool
        """
        self.ensure_one()
        return (
            self.state in ["draft", "sent"]
            and not self.is_expired
            and self.require_signature
            and not self.signature
        )

    def _has_to_be_paid(self):
        """Determine whether a sale order has to be paid.

        A sale order has to be paid when:
        - its state is 'draft' or 'sent';
        - it is not expired;
        - the prepayment percent is strictly positive;
        - the total amount is strictly positive.
        - confirmation amount is not reached

        Note: self.ensure_one()

        :return: Whether the sale order has to be paid.
        :rtype: bool
        """
        self.ensure_one()
        return (
            self.state in ["draft", "sent"]
            and not self.is_expired
            and self.prepayment_percent > 0
            and self.amount_total > 0
            and not self._is_confirmation_amount_reached()
        )

    def _get_portal_return_action(self):
        """Return the action used to display orders when returning from customer portal."""
        self.ensure_one()
        return self.env.ref("sale.action_quotations_with_onboarding")

    def _get_name_portal_content_view(self):
        """Determine the template used to display the SO on the portal.

        This method can be inherited by localizations who want to localize the online quotation
        view.
        """
        self.ensure_one()
        return "sale.sale_order_portal_content"

    def _get_name_tax_totals_view(self):
        """Determine the template used to display tax totals.

        This method can be inherited by localizations who want to localize the taxes displayed on
        the portal and sale order report.
        """
        return "sale.document_tax_totals"

    def _get_report_base_filename(self):
        self.ensure_one()
        return f"{self.type_name} {self.name}"

    # === CORE METHODS OVERRIDES ===#

    @api.model
    def get_empty_list_help(self, help_message):
        self = self.with_context(empty_list_help_document_name=self.env._("sale order"))
        return super().get_empty_list_help(help_message)

    def _compute_field_value(self, field):
        if field.name != "invoice_status" or self.env.context.get("mail_activity_automation_skip"):
            return super()._compute_field_value(field)

        filtered_self = self.filtered(
            lambda so: (
                so.ids
                and (so.user_id or so.partner_id.user_id)
                and so._origin.invoice_status != "upselling"
            )
        )
        super()._compute_field_value(field)

        upselling_orders = filtered_self.filtered(lambda so: so.invoice_status == "upselling")
        upselling_orders._create_upsell_activity()

    # === BUSINESS METHODS ===#

    def _create_upsell_activity(self):
        if not self:
            return

        self.activity_unlink(["mail.mail_activity_data_todo"])
        for order in self:
            order_ref = order._get_html_link()
            customer_ref = order.partner_id._get_html_link()
            order.activity_schedule(
                "mail.mail_activity_data_todo",
                user_id=order.user_id.id or order.partner_id.user_id.id,
                note=order.env._(
                    "Upsell %(order)s for customer %(customer)s",
                    order=order_ref,
                    customer=customer_ref,
                ),
            )

    def _prepare_analytic_account_data(self, prefix=None, plan_id=None):
        """Prepare SO analytic account creation values.

        :return: `account.analytic.account` creation values
        :rtype: dict
        """
        self.ensure_one()
        name = self.name
        if prefix:
            name = prefix + ": " + self.name
        if not plan_id:
            plan_id, _other_plans = self.env["account.analytic.plan"]._get_all_plans()
        return {
            "name": name,
            "code": self.client_order_ref,
            "company_id": self.company_id.id,
            "plan_id": plan_id.id,
            "partner_id": self.partner_id.id,
        }

    def _prepare_down_payment_section_line(self, **optional_values):
        """Prepare the values to create a new down payment section.

        :param dict optional_values: any parameter that should be added to the creation values
        :return: `account.move.line` creation values
        :rtype: dict
        """
        self.ensure_one()
        context = {"lang": self.partner_id.lang}
        down_payments_section_line = {
            "display_type": "line_section",
            "name": self.env._("Down Payments"),
            "product_id": False,
            "product_uom_id": False,
            "quantity": 0,
            "discount": 0,
            "price_unit": 0,
            "account_id": False,
            **optional_values,
        }
        del context
        return down_payments_section_line

    def _create_down_payment_lines_from_base_lines(self, down_payment_base_lines):
        """Add the base lines passed as parameter as sale order lines into the current sale order.

        :param down_payment_base_lines: A list of base lines
                                        (see '_prepare_base_line_for_taxes_computation').
        :return The newly created SO lines.
        """
        self.ensure_one()
        sequence = max(self.order_line.mapped("sequence") or [10]) + 1
        return (
            self
            .env["sale.order.line"]
            .with_context(sale_no_log_for_new_lines=True)
            .create([
                {
                    **self._prepare_down_payment_line_values_from_base_line(base_line),
                    "sequence": sequence + index,
                }
                for index, base_line in enumerate(down_payment_base_lines)
            ])
        )

    def _create_down_payment_section_line_if_needed(self):
        """Add the down section line if not already there on the current SO.

        :return The newly created SO line or None if the section was already there.
        """
        self.ensure_one()
        # If a down payment is already there, then the section is not needed and
        # has already been created.
        if any(line.display_type and line.is_downpayment for line in self.order_line):
            return None

        sequence = max(self.order_line.mapped("sequence") or [10]) + 1
        return (
            self
            .env["sale.order.line"]
            .with_context(sale_no_log_for_new_lines=True)
            .create({**self._prepare_down_payment_line_section_values(), "sequence": sequence})
        )

    def _prepare_down_payment_line_section_values(self):
        """Prepare the values to create a section line for the down payment on the current SO.

        :return: A dictionary to create a new SO section line.
        """
        self.ensure_one()
        return {"order_id": self.id, "display_type": "line_section", "is_downpayment": True}

    def _prepare_down_payment_line_values_from_base_line(self, base_line):
        """Convert the base line passed as parameter representing a down payment into a
        dictionary to be converted into a sale order line in the current sale order.

        :param base_line: A base line (see '_prepare_base_line_for_taxes_computation').
        :return: A dictionary to create a new SO line.
        """
        self.ensure_one()
        extra_tax_data = self.env["account.tax"]._export_base_line_extra_tax_data(base_line)
        return {
            "order_id": self.id,
            "is_downpayment": True,
            "product_uom_qty": 0.0,
            "price_unit": base_line["price_unit"],
            "tax_ids": [Command.set(base_line["tax_ids"].ids)],
            "analytic_distribution": base_line["analytic_distribution"],
            "extra_tax_data": extra_tax_data,
        }

    def _get_prepayment_required_amount(self):
        """Return the default prepayment amount.

        :return: The default prepayment amount.
        :rtype: float
        """
        self.ensure_one()

        if self.prepayment_percent == 0:
            return 0
        return self.currency_id.round(self.amount_total * self.prepayment_percent)

    def _is_confirmation_amount_reached(self):
        """Return whether `self.amount_paid` is higher than the prepayment required amount.

        Note: self.ensure_one()

        If no prepayment is required, the full amount must be paid to prevent partial (e.g., split)
        payments from confirming the order.

        :return: Whether `self.amount_paid` is higher than the prepayment required amount.
        :rtype: bool
        """
        self.ensure_one()
        if not self.prepayment_percent:
            return self._is_paid()
        amount_comparison = self.currency_id.compare_amounts(
            self._get_prepayment_required_amount(), self.amount_paid
        )
        return amount_comparison <= 0

    def _generate_downpayment_invoices(self):
        """Generate invoices as down payments for sale order.

        :return: The generated down payment invoices.
        :rtype: recordset of `account.move`
        """
        generated_invoices = self.env["account.move"]

        for order in self:
            downpayment_wizard = order.env["sale.advance.payment.inv"].create({
                "sale_order_ids": order,
                "advance_payment_method": "fixed",
                "fixed_amount": self.env.context.get("downpayment_fixed_amount", order.amount_paid),
            })
            generated_invoices |= downpayment_wizard._create_invoices(order)

        return generated_invoices

    def deliver_sold_quantity(self):
        invalid_targets = self.filtered(
            lambda o: (
                o.state != "sale"
                or any(line.qty_delivered_method != "manual" for line in o.order_line)
            )
        )
        if invalid_targets:
            raise UserError(
                self.env._(
                    "The following sale orders %(invalid_orders)s can't be delivered. Cancelled all"
                    " deliveries.",
                    invalid_orders=", ".join(invalid_targets.mapped("name")),
                )
            )
        for order in self:
            for line in order.order_line:
                line.qty_delivered = line.product_uom_qty

    def _get_or_create_analytic_account(self, plan_id):
        self.ensure_one()

        if not self.analytic_account_id:
            self.analytic_account_id = self.env["account.analytic.account"].create(
                self._prepare_analytic_account_data(plan_id=plan_id)
            )
        return self.analytic_account_id

    # === CATALOG === #

    def _get_action_add_from_catalog_extra_context(self):
        return {
            **super()._get_action_add_from_catalog_extra_context(),
            "order_customer_id": self.partner_id.id,
            "product_catalog_digits": self.order_line._fields["price_unit"].get_digits(self.env),
        }

    def _get_catalog_currency(self):
        return self.currency_id or super()._get_catalog_currency()

    def _get_product_catalog_domain(self):
        products = Domain(self.env["sale.order.line"]._domain_product_id())
        attachments = self.env.ref("sale.jingneng_product_category_attachment", raise_if_not_found=False)
        if attachments:
            products |= (Domain("active", "=", True) & Domain("sale_ok", "=", True)
                         & Domain("categ_id", "child_of", attachments.id))
        return super()._get_product_catalog_domain() & products

    def _get_product_catalog_default_prices(self, products, **kwargs) -> dict:
        # Override the default price computation to consider the pricelist prices instead.
        return self.pricelist_id._get_products_price(
            quantity=1.0,
            products=products,
            currency=self._get_catalog_currency(),
            date=self.date_order,
            **kwargs,
        )

    def _is_readonly(self) -> bool:
        """Return whether the sale order is read-only or not based on the state or the lock status.

        A sale order is considered read-only if its state is 'cancel' or if the sale order is
        locked.
        """
        return super()._is_readonly() or self.state == "cancel" or self.locked

    def _get_product_catalog_product_data(self, product, **kwargs) -> dict:
        product_data = super()._get_product_catalog_product_data(product, **kwargs)
        has_warning_group = self.env["res.groups"]._is_feature_enabled("sale.group_warning_sale")
        if product.sale_line_warn_msg and has_warning_group:
            product_data.update(warning=product.sale_line_warn_msg)
        return product_data

    def _has_sections(self) -> bool:
        return True

    # === Product Documents === #

    def _get_product_documents(self):
        self.ensure_one()

        documents = (
            self.order_line.product_id.product_document_ids
            | self.order_line.product_template_id.product_document_ids
        )
        return self._filter_product_documents(documents).sorted()

    def _filter_product_documents(self, documents):
        return documents.filtered(
            lambda document: (
                document.attached_on_sale == "quotation"
                or (self.state == "sale" and document.attached_on_sale == "sale_order")
            )
        )

    # === TOOLING ===#

    def _is_partially_paid(self):
        """Return whether the sales order has a confirmed partial payment.

        The order is not considered "partially" paid if it's fully paid.

        :return: Whether the order is partially paid
        :rtype: bool
        """
        self.ensure_one()

        if self.state == "cancel":
            return False  # Allow resetting the cart when it's canceled from the backend

        if self._is_paid():
            return False

        return any(self.transaction_ids.filtered(lambda tx: tx.state in {"authorized", "done"}))

    def _is_paid(self):
        """Return whether the sale order is paid or not based on the linked transactions.

        A sales order is considered paid if the sum of all the paid (i.e., `authorized` or `done`)
        transactions' amount is equal to or higher than `self.amount_total`.

        :return: Whether the order is considered paid
        :rtype: bool
        """
        self.ensure_one()
        return self.currency_id.compare_amounts(self.amount_paid, self.amount_total) >= 0

    def _check_not_partially_paid(self):
        """Check if the order is partially paid, and raises an error if so.

        :rtype: None
        :raise UserError: If the error is partially paid
        """
        if self._is_partially_paid():
            raise UserError(
                self.env._(
                    "It seems that there is already a payment in progress for your order; you can't"
                    " modify it anymore."
                )
            )

    def _get_lang(self):
        self.ensure_one()

        if self.partner_id.lang and not self.partner_id.is_public:
            return self.partner_id.lang

        return self.env.lang

    @api.model
    def get_import_templates(self):
        return [
            {
                "label": self.env._("Template for Quotations"),
                "template": "/sale/static/xls/quotations_import_template.xlsx?v=jingneng-quote2-blank",
            }
        ]


    @api.model
    def _read_custom_import_xlsx(self, book, options):
        """Read the public A4 sheet exported by the Jingneng quote program."""
        sheet_name = options.get("sheet") or ("报价2" if "报价2" in book.sheetnames else None)
        if not sheet_name:
            return None
        sheet = book[sheet_name]
        quote_headers = ("序号", "名称", "规格型号(W*D*H)", "数量", "单位", "折后单价", "折后总价", "备注")
        if tuple(cell.value for cell in sheet[10][:8]) != quote_headers:
            return None
        number_label = sheet["D1"].value
        if number_label not in (None, "编号") or (sheet["D2"].value, sheet["A4"].value) != ("日期", "买方"):
            return None
        options["sheet"] = sheet_name

        def text(value):
            return "" if value is None else str(value).strip()

        number = text(sheet["E1"].value) if number_label else ""
        customer = text(sheet["B4"].value)
        if not customer or not sheet["E2"].value:
            raise ValueError("请在报价表的 E2 和 B4 分别填写日期和买方。")
        try:
            quote_date = fields.Date.to_date(sheet["E2"].value)
        except (TypeError, ValueError) as error:
            raise ValueError("报价日期 E2 应使用 YYYY-MM-DD 格式。") from error

        company = self.env.company
        currency = company.currency_id
        if currency.name != "CNY":
            raise ValueError("报价2 的金额以人民币计价，请在人民币公司中导入。")

        order_columns = ["partner_id", "date_order"]
        order_values = [customer, quote_date]
        if number:
            order_columns.append("client_order_ref")
            order_values.append(number)
        order_fields = self.fields_get([*order_columns, "order_line"], ["string"])
        detail_fields = self.env["sale.order.line"].fields_get(
            ["product_id", "name", "product_uom_qty", "price_unit"], ["string"],
        )
        order_caption = order_fields["order_line"]["string"]
        rows = [[order_fields[key]["string"] for key in order_columns] + [
            f"{order_caption}/{detail_fields[key]['string']}"
            for key in ("product_id", "name", "product_uom_qty", "price_unit")
        ]]
        products = {}
        amounts = []
        for row_number, cells in enumerate(sheet.iter_rows(min_row=11, max_col=8, values_only=True), 11):
            index, name, specification, quantity, unit, unit_price, total, remark = cells
            if not any(value is not None and value != "" for value in cells):
                continue
            if text(name) == "合计" and not text(index):
                break
            if not all(text(value) for value in (index, name, specification, unit)):
                raise ValueError(f"报价表第 {row_number} 行缺少序号、名称、规格或单位。")
            try:
                quantity, unit_price, total = map(float, (quantity, unit_price, total))
                if not all(map(math.isfinite, (quantity, unit_price, total))) or quantity <= 0:
                    raise ValueError
            except (TypeError, ValueError) as error:
                raise ValueError(f"报价表第 {row_number} 行的数量、单价或金额无效。请用 Excel 重新计算并保存。") from error
            if currency.compare_amounts(quantity * unit_price, total):
                raise ValueError(f"报价表第 {row_number} 行的折后总价与数量乘单价不一致。")

            unit = text(unit)
            if unit not in products:
                code = "AIQUOTE-IMPORT-" + sha256(unit.encode()).hexdigest()[:12].upper()
                product = self.env["product.product"].search([
                    ("default_code", "=", code), ("sale_ok", "=", True),
                    ("company_id", "in", [False, company.id]),
                ], limit=2)
                if len(product) != 1 or product.uom_id.name != unit:
                    raise ValueError(f"当前公司缺少用于导入「{unit}」的京能报价明细产品。")
                if product.taxes_id._filter_taxes_by_company():
                    raise ValueError(f"京能报价明细产品「{unit}」设有销售税，导入会改变报价原金额。")
                products[unit] = product.display_name
            description = f"{text(name)}\n规格：{text(specification)}"
            if text(remark):
                description += f"\n{text(remark)}"
            order = order_values if not amounts else [""] * len(order_columns)
            rows.append(order + [products[unit], description, str(quantity), str(unit_price)])
            amounts.append(total)
        else:
            raise ValueError("报价表缺少合计行。")

        if not amounts:
            raise ValueError("请至少填写一条报价明细。")
        try:
            subtotal = float(total)
        except (TypeError, ValueError) as error:
            raise ValueError("报价合计无效。请用 Excel 重新计算并保存。") from error
        if not math.isfinite(subtotal) or currency.compare_amounts(math.fsum(amounts), subtotal):
            raise ValueError("报价表的合计与折后明细金额不一致。")
        return len(rows), rows

    @api.model
    def batch_onchange_sol(self, lines_data, order_changes, fields_spec):
        """Batch `sale.order.line` onchange calls into a single RPC.

        :param dict lines_data: {line_id: {"ids", "changes", "field_name"}}, one entry per line
        :param dict fields_spec: onchange fields spec, shared by every line
        :return: {line_id: recomputed values}
        :rtype: dict
        """
        SaleOrderLine = self.env["sale.order.line"]
        result = {}
        for line_id, data in lines_data.items():
            onchange_values = {**data["changes"], **order_changes}
            values = (
                SaleOrderLine
                .browse(data["ids"])
                .onchange(onchange_values, data["changed_fields"], fields_spec)
                .get("value", {})
            )
            result[line_id] = values
        return result

    # For `sale_management`, to control optional products on portal
    def _can_be_edited_on_portal(self):
        self.ensure_one()
        return self.state in ("draft", "sent")

    # === DASHBOARD METHODS ===#

    @api.model
    @api.readonly
    def get_views(self, views, options=None):
        """Keep the pivot routing consistent when a tab restores an older action."""
        action_id = (options or {}).get("action_id")
        if action_id and any(view_type == "pivot" for _, view_type in views):
            quotations = self.env.ref("sale.action_quotations_with_onboarding", raise_if_not_found=False)
            orders = self.env.ref("sale.action_orders", raise_if_not_found=False)
            progress = self.env.ref("sale.view_quotation_progress", raise_if_not_found=False)
            pivot = self.env.ref("sale.view_sale_order_pivot", raise_if_not_found=False)
            if progress and pivot:
                if orders and action_id == orders.id:
                    target, previous = progress.id, pivot.id
                elif quotations and action_id == quotations.id:
                    target, previous = pivot.id, progress.id
                else:
                    return super().get_views(views, options=options)
                views = [
                    (target, view_type) if view_type == "pivot" and view_id in (False, None, previous)
                    else (view_id, view_type)
                    for view_id, view_type in views
                ]
        return super().get_views(views, options=options)

    @api.model
    @api.readonly
    def retrieve_quotation_progress(self, domain, start, end, status=False, offset=0):
        """Return accessible orders with an event in the period or an open quotation."""
        start, end = fields.Datetime.to_datetime(start), fields.Datetime.to_datetime(end)
        if not start or not end or not 27 <= (end - start).days <= 32:
            raise UserError("请选择一个有效的月份。")
        event_fields = (
            "create_date", "quotation_prepared_at", "quotation_sent_at", "quotation_contract_at",
            "signed_on", "quotation_confirmed_at", "date_order", "commitment_date",
        )
        relevant = Domain.OR(
            Domain(field, ">=", start) & Domain(field, "<", end) for field in event_fields
        )
        source_start = fields.Datetime.context_timestamp(self, start).date()
        source_end = fields.Datetime.context_timestamp(self, end).date()
        source_in_month = Domain("quotation_source_date", ">=", source_start) & Domain(
            "quotation_source_date", "<", source_end
        )
        base = Domain(domain) & (
            (Domain("create_date", "<", end) & (Domain("state", "in", ["draft", "sent"]) | relevant))
            | source_in_month
        )
        counts = dict(self._read_group(base, ["quotation_status"], ["__count"]))
        query = base & Domain("quotation_status", "=", status) if status else base
        limit = 8
        rows = self.search_read(
            query,
            ["name", "partner_id", "quotation_status", "state", "is_unfulfilled", "quotation_source_date", *event_fields],
            offset=max(0, int(offset)), limit=limit, order="date_order desc, id desc",
        )
        return {"rows": rows, "counts": counts, "limit": limit}

    @api.model
    def retrieve_sale_dashboard(self, period_days):
        """Retrieve statistics for the Sales dashboard for a given period (number of days).

        :param int period_days: The number of days used to filter orders.
        :return: A dictionary containing all computed dashboard statistics.
        :rtype: dict
        """
        dashboard_data = {
            "sales_amount": {"total": 0, "gain": None},
            "orders": {"total": 0, "gain": None},
            "to_upsell": 0,
            "to_fulfill": 0,
            "to_confirm": 0,
            "to_invoice": 0,
            "currency_id": self.env.company.currency_id.id,
        }

        confirmed_domain = Domain("state", "=", "sale")

        current_totals = self._get_sale_period_totals(period_days, confirmed_domain)
        previous_totals = self._get_sale_period_totals(period_days, confirmed_domain, previous=True)

        self._fill_dashboard_gains(dashboard_data, current_totals, previous_totals)

        dashboard_data.update({
            "to_upsell": self.search_count(
                Domain("invoice_status", "=", "upselling") & confirmed_domain
            ),
            "to_fulfill": self.search_count(Domain("is_unfulfilled", "=", True) & confirmed_domain),
            "to_confirm": self.search_count(Domain("quotation_status", "=", "contract")),
            "to_invoice": self.search_count(
                Domain("invoice_status", "=", "to invoice") & confirmed_domain
            ),
        })

        return dashboard_data

    def _fill_dashboard_gains(self, dashboard_data, current_totals, previous_totals):
        """Fill dashboard_data period entries with totals and gain percentages.

        :param dict dashboard_data: The dashboard dict to update in place.
        :param dict current_totals: Totals for the current period, keyed by metric name.
        :param dict previous_totals: Totals for the previous period, keyed by metric name.
        """
        for key, current_total in current_totals.items():
            previous_total = previous_totals[key]
            dashboard_data[key]["total"] = current_total
            dashboard_data[key]["gain"] = (
                float_round(
                    ((current_total - previous_total) / previous_total) * 100,
                    precision_rounding=0.01,
                )
                if previous_total
                else None
            )

    def _get_period_domain(self, field, period_days, previous=False):  # noqa: PLR6301
        """Build a domain to filter records for a given time period.

        :param str field: The datetime field used for date filtering.
        :param int period_days: Number of days representing the target period.
        :param bool previous: When True, returns a domain for the previous equivalent period.
        :return: Domain used to filter records by date range.
        """
        if not previous:
            return Domain(field, ">", f"today -{period_days}d +1d")
        return Domain(field, ">", f"today -{period_days * 2}d +1d") & Domain(
            field, "<", f"today -{period_days}d +1d"
        )

    def _get_sale_period_totals(self, period_days, extra_domain, previous=False):
        """Calculate aggregated order and sales statistics for a given period.

        :param int period_days: Number of days for the period.
        :param Domain extra_domain: Additional domain criteria.
        :param bool previous: When True, computes totals for the previous equivalent period.
        :return: A dictionary containing total orders and total sales for the given period.
        :rtype: dict
        """
        # sale.report exposes date_order as 'date'
        period_domain = self._get_period_domain("date", period_days, previous=previous)
        aggregated_data = self.env["sale.report"]._read_group(
            domain=period_domain & extra_domain,
            aggregates=["price_total:sum", "order_reference:count_distinct"],
        )
        total_sales, total_orders = aggregated_data[0]
        return {
            "orders": total_orders,
            "sales_amount": float_round(total_sales or 0.0, precision_rounding=0.01),
        }


DRAWING_PRODUCT_SERIES = [
    ("JA", "JA"), ("JC", "JC"), ("JE", "JE"), ("JK", "JK"), ("JM", "JM"),
    ("JP", "JP"), ("JP WIDE", "JP WIDE"), ("JQ", "JQ"),
    ("JS", "JS"), ("JS WIDE", "JS WIDE"), ("操作台", "操作台"),
]


class SaleOrderDrawing(models.Model):
    _name = "sale.order.drawing"
    _description = "报价单图纸与选型"
    _order = "id desc"
    _check_company_auto = True

    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="order_id.company_id", store=True, index=True)
    name = fields.Char(string="图纸文件", required=True)
    file_data = fields.Binary(string="图纸内容", required=True, attachment=True, copy=False)
    upload_files = fields.Json(string="待上传图纸", store=False, copy=False)
    file_kind = fields.Selection(
        [("pdf", "PDF"), ("image", "图片"), ("cad", "CAD")],
        compute="_compute_file_kind",
    )
    page_count = fields.Integer(string="图纸页数", readonly=True)
    page_label = fields.Char(string="页数", compute="_compute_page_label")
    state = fields.Selection(
        [("pending", "待选型"), ("confirmed", "已选型")],
        string="选型状态", default="pending", required=True, readonly=True, copy=False,
    )
    product_series = fields.Selection(DRAWING_PRODUCT_SERIES, string="产品类型")
    product_template_id = fields.Many2one(
        "product.template", string="产品主档", check_company=True,
        domain=lambda self: self.env["sale.order.line"]._domain_product_id(),
    )
    quantity = fields.Float(string="数量", default=1, digits="Product Unit")
    uom_id = fields.Many2one(related="product_template_id.uom_id", string="单位")
    width_mm = fields.Float(string="宽度（mm）", digits=(12, 2))
    width_expression = fields.Char(string="宽度（mm）", help="填写单宽或按柜位顺序用半角 + 分隔，例如 600+600。")
    cabinet_configuration = fields.Json(string="门型与柜位", copy=False)
    depth_mm = fields.Float(string="深度（mm）", digits=(12, 2))
    height_mm = fields.Float(string="高度（mm）", digits=(12, 2))
    material = fields.Char(string="材质", default="SECC")
    thickness_mm = fields.Float(
        string="箱体料厚（mm）", default=1.5, digits=(12, 3),
        help="箱体输入料厚；门板、安装板等板件按各自目录规则处理，不共用此料厚。")
    coating_type = fields.Selection(
        [("none","无"),("gloss","平光"),("orange","橘纹")],
        string="喷塑", default="orange",
        help="报价程序预置喷塑选项；颜色单独填写。")
    display_color = fields.Char(string="颜色", default="RAL7035 浅灰")
    orientation_data = fields.Json(string="图纸页面方向", copy=False, readonly=True)
    orientation_state = fields.Selection(
        [("queued","旋正排队中"),("processing","旋正中"),("ready","旋正完成"),("failed","旋正未完成")],
        default="queued", copy=False, readonly=True, index=True)
    orientation_started_at = fields.Datetime(copy=False, readonly=True)
    orientation_token = fields.Char(copy=False, readonly=True)
    dimension_assist_data = fields.Json(string="辅助尺寸复核", copy=False)
    door_type = fields.Char(string="门型")
    selection_notes = fields.Text(string="附件选配与技术要求")
    accessory_selection_data = fields.Json(string="附件选配", copy=False)
    source_text = fields.Text(string="图纸文字", readonly=True, copy=False)
    order_line_id = fields.Many2one(
        "sale.order.line", string="关联明细", ondelete="set null", check_company=True,
        domain="[('order_id', '=', order_id), ('display_type', '=', False)]",
    )
    reviewed_by = fields.Many2one("res.users", string="复核人员", readonly=True, copy=False)
    reviewed_at = fields.Datetime(string="复核时间", readonly=True, copy=False)
    selection_row_hidden = fields.Boolean(default=False, copy=False)
    can_edit_selection = fields.Boolean(compute="_compute_can_edit_selection")
    recognition_state = fields.Selection(
        [("idle", "待识别"), ("queued", "排队中"), ("processing", "识别中"),
         ("review", "待复核"), ("done", "已复核"), ("failed", "识别失败")],
        string="识别状态", default="idle", required=True, readonly=True, copy=False,
    )
    recognition_token = fields.Char(readonly=True, copy=False)
    recognition_started_at = fields.Datetime(readonly=True, copy=False)
    recognition_finished_at = fields.Datetime(string="识别完成时间", readonly=True, copy=False)
    recognition_data = fields.Json(readonly=True, copy=False)
    recognition_completed_pages = fields.Integer(string="已处理页数", readonly=True, copy=False)
    recognition_error = fields.Text(string="识别提示", readonly=True, copy=False)
    recognition_candidate_ids = fields.One2many(
        "sale.order.drawing.candidate", "drawing_id", string="全部识别项目", readonly=True, copy=False,
    )
    active_recognition_id = fields.Many2one(
        "sale.order.drawing.candidate", string="识别项目", ondelete="set null", copy=False,
        domain="[('drawing_id', '=', id), ('recognition_token', '=', recognition_token)]",
    )
    recognition_summary = fields.Text(related="active_recognition_id.evidence_summary", string="识别依据")
    drawing_recognition_pending = fields.Boolean(compute="_compute_recognition_progress")
    drawing_recognition_version = fields.Char(compute="_compute_recognition_progress")
    file_data_page = fields.Integer(related="active_recognition_id.page_number", string="原图页码")

    @api.depends("recognition_state", "recognition_token", "recognition_completed_pages", "state", "orientation_state", "orientation_data")
    def _compute_recognition_progress(self):
        for drawing in self:
            drawing.drawing_recognition_pending = drawing.recognition_state in ("queued", "processing") or drawing.orientation_state in ("queued", "processing")
            drawing.drawing_recognition_version = f"{drawing.recognition_state}:{drawing.recognition_token}:{drawing.recognition_completed_pages}:{drawing.orientation_state}:{len((drawing.orientation_data or {}).get("pages",{}))}:{drawing.state}"

    @api.readonly
    def get_drawing_recognition_progress(self):
        self.ensure_one()
        self.check_access("read")
        return {"version": self.drawing_recognition_version}

    @api.readonly
    def get_dimension_view(self, page, point, rotation=0, orientation=0, rect=None):
        """Read labels inside a manually selected view without writing dimensions."""
        self.ensure_one()
        self.check_access("read")
        if self.file_kind not in ("pdf", "image"):
            raise UserError("请在 PDF 或图片预览中拖动框选视图。")
        if (type(page) is not int or not 1 <= page <= max(1, self.page_count)
                or rotation not in (0, 90, 180, 270) or orientation not in (0, 90, 180, 270)
                or not isinstance(point, list) or len(point) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in point)):
            raise ValidationError("图纸页码或点击坐标无效。")
        if self.file_kind == "image" and page != 1:
            raise ValidationError("请点选当前预览的图片页。")
        if rect is not None and (
                not isinstance(rect,list) or len(rect)!=4
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in rect)
                or min(rect[:2])<0 or min(rect[2:])<.005
                or rect[0]+rect[2]>1.000001 or rect[1]+rect[3]>1.000001):
            raise ValidationError("框选范围无效，请重新拖动框选。")
        settings = self._recognition_configuration()
        content = self.file_data.content
        import psutil
        with TemporaryDirectory(prefix="dimension-", dir=settings["workspace"]) as folder:
            path = Path(folder) / ("drawing" + PurePath(self.name).suffix.lower())
            path.write_bytes(content)
            payload = {**settings, "path": str(path), "page": page, "point": point,
                       "rotation": rotation, "orientation": orientation, "rect": rect}
            process = subprocess.Popen(
                [sys.executable, "-c", _DRAWING_DIMENSION_WORKER], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ,"PYTHONIOENCODING":"utf-8","PYTHONDONTWRITEBYTECODE":"1"},
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),
            )
            try:
                stdout, stderr = process.communicate(json.dumps(payload,ensure_ascii=False).encode("utf-8"),timeout=55)
                prefix=b"ODOO_DIMENSION_RESULT="
                line=next((line for line in reversed(stdout.splitlines()) if line.startswith(prefix)),None)
                if process.returncode or line is None:
                    _logger.warning("Dimension reading failed for drawing %s: %s",self.id,stderr.decode("utf-8",errors="replace")[-1800:])
                    raise UserError("当前视图读取未完成，请重新框选或手工填写尺寸。")
                output=json.loads(line[len(prefix):])
                if output.get("error"):
                    raise UserError(output["error"])
                output["file_sha256"]=sha256(content).hexdigest()
                output["candidate_id"]=self.active_recognition_id.id or 0
                return output
            except subprocess.TimeoutExpired as error:
                raise UserError("本次尺寸读取超时，请重试或手工填写。") from error
            finally:
                if process.poll() is None:
                    for child in reversed(psutil.Process(process.pid).children(recursive=True)):
                        try: child.kill()
                        except psutil.NoSuchProcess: pass
                    process.kill()
                    process.communicate(timeout=10)

    @api.readonly
    def get_drawing_orientation(self, page=1):
        self.ensure_one()
        self.check_access("read")
        if (self.file_kind not in ("pdf","image") or type(page) is not int
                or not 1<=page<=max(1,self.page_count) or self.file_kind=="image" and page!=1):
            raise ValidationError("图纸页码无效。")
        data=self.orientation_data or {}
        result=(data.get("pages") or {}).get(str(page))
        return {**(result or {"angle":None,"confirmed":False}),
                "state":"ready" if result else self.orientation_state,
                "page":page}

    def _compute_drawing_orientation(self, digest, page):
        settings=self._recognition_configuration()
        import psutil
        with TemporaryDirectory(prefix="orientation-",dir=settings["workspace"]) as folder:
            path=Path(folder)/("drawing"+PurePath(self.name).suffix.lower())
            path.write_bytes(self.file_data.content)
            payload={**settings,"path":str(path),"page":page}
            process=subprocess.Popen([sys.executable,"-c",_DRAWING_ORIENTATION_WORKER],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                env={**os.environ,"PYTHONIOENCODING":"utf-8","PYTHONDONTWRITEBYTECODE":"1"},
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            try:
                stdout,stderr=process.communicate(json.dumps(payload,ensure_ascii=False).encode("utf-8"),timeout=55)
                prefix=b"ODOO_ORIENTATION_RESULT="
                line=next((line for line in reversed(stdout.splitlines()) if line.startswith(prefix)),None)
                if process.returncode or line is None:
                    _logger.warning("Drawing orientation failed for drawing %s: %s",self.id,stderr.decode("utf-8",errors="replace")[-1200:])
                    raise UserError("当前页自动旋正未完成，可以继续查看原图。")
                result=json.loads(line[len(prefix):]);result["file_sha256"]=digest;result["page"]=page
                return result
            except subprocess.TimeoutExpired as error:
                raise UserError("当前页自动旋正超时，可以继续查看原图。") from error
            finally:
                if process.poll() is None:
                    for child in reversed(psutil.Process(process.pid).children(recursive=True)):
                        try:child.kill()
                        except psutil.NoSuchProcess:pass
                    process.kill();process.communicate(timeout=10)

    @api.model
    def _cron_orient_drawings(self):
        stale=self.search([("orientation_state","=","processing"),
            ("orientation_started_at","<",fields.Datetime.now()-timedelta(minutes=5))])
        if stale:
            super(SaleOrderDrawing,stale).write({"orientation_state":"queued"})
        self.env["ir.cron"]._commit_progress(remaining=self.search_count([("orientation_state","=","queued")]))
        while True:
            self.env.cr.execute("SELECT id FROM sale_order_drawing WHERE orientation_state='queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1")
            row=self.env.cr.fetchone()
            if not row:return
            drawing=self.browse(row[0])
            token=drawing.orientation_token
            digest=sha256(drawing.file_data.content).hexdigest()
            pages=max(1,drawing.page_count) if drawing.file_kind=="pdf" else 1
            super(SaleOrderDrawing,drawing).write({"orientation_state":"processing","orientation_started_at":fields.Datetime.now()})
            self.env["ir.cron"]._commit_progress()
            for page in range(1,pages+1):
                try:
                    result=drawing._compute_drawing_orientation(digest,page) if drawing.file_kind in ("pdf","image") else {"angle":None,"confirmed":False}
                except Exception:
                    _logger.exception("Background orientation failed for drawing %s page %s",drawing.id,page)
                    result={"angle":None,"confirmed":False,"error":True}
                # A replacement/deletion while the subprocess runs must never
                # publish old directions onto the new attachment.
                self.env.cr.commit();drawing.invalidate_recordset()
                if not drawing.exists() or drawing.orientation_token!=token:break
                data=drawing.orientation_data or {"file_sha256":digest,"pages":{}}
                data["pages"][str(page)]=result
                super(SaleOrderDrawing,drawing).write({"orientation_data":data,
                    "orientation_state":"ready" if page==pages else "processing"})
                self.env["ir.cron"]._commit_progress()
            if drawing.exists() and drawing.recognition_state=="queued":
                self.env.ref("sale.ir_cron_drawing_recognition")._trigger()
            if not self.env["ir.cron"]._commit_progress(1):return

    @staticmethod
    def _accessory_name_overlap(name, text):
        # Coverage is relative to the accessory name, not the entire drawing.
        # Count Chinese characters with multiplicity, independently of order;
        # numbers/model codes do not inflate the score.
        name_chars=re.findall(r"[\u3400-\u9fff]",unicodedata.normalize("NFKC",str(name or "")))
        text_counts=Counter(re.findall(r"[\u3400-\u9fff]",unicodedata.normalize("NFKC",str(text or ""))))
        common=Counter(name_chars)&text_counts
        count=sum(common.values())
        total=len(name_chars)
        if not total or count*100<total*60:
            return None
        evidence=[]
        for char in name_chars:
            if common[char]:
                evidence.append(char)
                common[char]-=1
        return {"text":"".join(evidence),"matched_count":count,
                "name_count":total,"rate":round(count*100/total,1)}

    def _drawing_accessory_catalog(self):
        self.ensure_one()
        return self._accessory_catalog_for_company(self.company_id)

    @api.model
    def _accessory_catalog_for_company(self, company):
        categories=self.env["product.category"].search([("name","=","附件")])
        products=self.env["product.template"].search([
            ("categ_id","child_of",categories.ids),("active","=",True),
            ("company_id","in",[False,company.id]),
        ],order="name,id") if categories else self.env["product.template"]
        catalog={}
        for product in products:
            item=catalog.setdefault(product.name,{
                "name":product.name,"category":product.jingneng_catalog_series or "附件","product_ids":[],
            })
            item["product_ids"].append(product.id)
        if not catalog:
            # Reuse the catalog already handed over for this installation.
            # Keep every variant as provenance; a name match is not a price match.
            path = self.env["ir.config_parameter"].sudo().get_str("sale.selection_accessory_catalog_path")
            if path and Path(path).is_file():
                try:
                    saved = json.loads(Path(path).read_text(encoding="utf-8-sig"))
                    if saved.get("schema") != 1 or not isinstance(saved.get("items"), list):
                        raise ValueError("Invalid accessory catalog schema")
                    for row in saved["items"]:
                        name = str(row.get("item_name") or "").strip()
                        if not name:
                            continue
                        item = catalog.setdefault(name, {"name": name,
                            "category": row.get("category_level1") or "附件", "product_ids": [],
                            "catalog_version": saved.get("source", {}).get("catalog_version"),
                            "catalog_rows": []})
                        item["catalog_rows"].append(row)
                except (OSError, ValueError, TypeError):
                    _logger.exception("Unable to read the saved accessory catalog")
        return catalog

    @api.readonly
    def get_drawing_accessory_catalog(self):
        self.ensure_one()
        self.check_access("read")
        return list(self._drawing_accessory_catalog().values())

    @api.readonly
    def get_drawing_accessories(self, candidate_id=0, manual_names=None):
        """Match readable drawing text against current accessory catalog names."""
        self.ensure_one()
        self.check_access("read")
        if type(candidate_id) is not int or candidate_id<0:
            raise ValidationError("识别项目无效。")
        candidate=self.env["sale.order.drawing.candidate"].browse(candidate_id).exists()
        if candidate_id and (not candidate or candidate.drawing_id!=self
                             or candidate.recognition_token!=self.recognition_token):
            raise ValidationError("请选择当前图纸的识别项目。")
        raw=(candidate.raw_result or {}) if candidate else {}
        text=raw.get("raw_text") or raw.get("text")
        if not text:
            pages=(self.recognition_data or {}).get("pages",[])
            if candidate:
                pages=[page for page in pages if page.get("source_page_number")==candidate.page_number]
            text="\n".join(str(page.get("raw_text") or page.get("text") or "") for page in pages)
        text=text or self.source_text or ""
        catalog=self._drawing_accessory_catalog()
        saved=self.accessory_selection_data or {}
        saved=saved if saved.get("candidate_id",0)==candidate_id else {}
        choices=saved.get("choices",{})
        manual_names=saved.get("manual_names",[]) if manual_names is None else manual_names
        if (not isinstance(manual_names,list) or len(manual_names)>1000
                or any(not isinstance(name,str) for name in manual_names)):
            raise ValidationError("手动附件数据无效，请从附件库重新选择。")
        manual_names=set(manual_names)
        items=[]
        for name,entry in catalog.items():
            overlap=self._accessory_name_overlap(name,text)
            if overlap or name in manual_names:
                items.append({**entry,"overlap":overlap["text"] if overlap else "",
                              "match_rate":overlap["rate"] if overlap else None,
                              "matched_count":overlap["matched_count"] if overlap else 0,
                              "name_count":overlap["name_count"] if overlap else 0,
                              "source":"manual" if name in manual_names else "drawing",
                              "selected":choices.get(name,True)})
        return {"items":items,"candidate_id":candidate_id,
                "pending":self.recognition_state in ("queued","processing"),
                "recognition_token":self.recognition_token}

    @api.constrains("accessory_selection_data")
    def _check_accessory_selection_data(self):
        for drawing in self.filtered("accessory_selection_data"):
            self._validate_accessory_selection_data(drawing.accessory_selection_data)

    @api.model
    def _validate_accessory_selection_data(self, data):
        if (not isinstance(data,dict) or data.get("version")!=1
                or type(data.get("candidate_id",0)) is not int
                or data.get("candidate_id",0)<0
                or not isinstance(data.get("choices"),dict)
                or not isinstance(data.get("manual_names",[]),list)
                or any(not isinstance(name,str) or not name.strip()
                       for name in data.get("manual_names",[]))
                or len(data.get("manual_names",[]))>1000
                or len(json.dumps(data))>100000
                or any(not isinstance(name,str) or type(selected) is not bool
                       for name,selected in data["choices"].items())):
            raise ValidationError("附件选配数据无效，请重新勾选。")

    @api.constrains("dimension_assist_data")
    def _check_dimension_assist_data(self):
        for drawing in self.filtered("dimension_assist_data"):
            data=drawing.dimension_assist_data
            if (not isinstance(data,dict) or data.get("version") != 1
                    or len(json.dumps(data)) > 100000):
                raise ValidationError("辅助尺寸复核数据无效。")

    @api.depends("name")
    def _compute_file_kind(self):
        for drawing in self:
            extension = PurePath(drawing.name or "").suffix.lower()
            drawing.file_kind = (
                "pdf" if extension == ".pdf" else
                "cad" if extension in (".dwg", ".dxf") else "image"
            )

    @api.depends("page_count")
    def _compute_page_label(self):
        for drawing in self:
            drawing.page_label = f"{drawing.page_count} 页" if drawing.page_count else "—"

    @api.depends("order_id.state", "order_id.quotation_status", "order_id.locked")
    def _compute_can_edit_selection(self):
        for drawing in self:
            drawing.can_edit_selection = (
                drawing.order_id.state in ("draft", "sent")
                and drawing.order_id.quotation_status != "contract"
                and not drawing.order_id.locked
            )

    def _check_orders_editable(self, orders):
        orders.check_access("write")
        if any(order.state not in ("draft", "sent")
               or order.quotation_status == "contract" or order.locked for order in orders):
            raise UserError("当前单据已锁定，请在可编辑的报价单中修改图纸与选型。")

    @api.model
    def _series_from_product(self, product):
        cabinet = self.env.ref("sale.jingneng_product_category_cabinet", raise_if_not_found=False)
        if product:
            series = product.jingneng_catalog_series
            if product.categ_id == cabinet and series in dict(DRAWING_PRODUCT_SERIES):
                return series
            # The installed demonstration predates the classified product catalog.
            legacy = (product.default_code or "").removeprefix("JN-DEMO-")
            if product.default_code in ("JN-DEMO-JA", "JN-DEMO-JP"):
                return legacy
        return False

    def _prepare_product_series_values(self, vals):
        values = dict(vals)
        if "product_series" not in values and "product_template_id" not in values:
            return values
        if "product_template_id" in values:
            product = self.env["product.template"].browse(values["product_template_id"]).exists()
        else:
            product = self.product_template_id if len(self) == 1 else self.env["product.template"]
        series = self._series_from_product(product)
        chosen = values.get("product_series")
        if chosen:
            if chosen not in dict(DRAWING_PRODUCT_SERIES):
                raise ValidationError("请选择已有的 11 个选型系列。")
            if chosen != series:
                values["product_template_id"] = False
        elif values.get("product_template_id"):
            if not series:
                raise ValidationError("产品类型请选择 JA、JC、JE、JK、JM、JP、JP WIDE、JQ、JS、JS WIDE 或操作台。")
            values["product_series"] = series
        elif "product_series" in values:
            values["product_template_id"] = False
        else:
            values["product_series"] = False
        return values

    @api.model_create_multi
    def create(self, vals_list):
        # Keep the native form's one-record create contract. Additional drawings
        # are created in this same transaction, so a bad file rolls back the batch.
        prepared, batches = [], []
        for original in vals_list:
            vals = dict(original)
            uploads = vals.pop("upload_files", None)
            if uploads:
                if not isinstance(uploads,list) or any(
                        not isinstance(item,dict) or not isinstance(item.get("name"),str)
                        or not item["name"].strip() or not isinstance(item.get("content"),str)
                        or not item["content"] for item in uploads):
                    raise ValidationError("待上传文件数据无效，请重新选择图纸。")
                first = uploads[0]
                vals.update(name=first["name"],file_data={"filename":first["name"],"content":first["content"]})
            prepared.append(self._prepare_cabinet_values(self._prepare_product_series_values(vals)))
            batches.append(uploads[1:] if uploads else [])
        vals_list = prepared
        orders = self.env["sale.order"].browse([
            vals.get("order_id") or self.env.context.get("default_order_id") for vals in vals_list
        ])
        self._check_orders_editable(orders)
        records = super().create([
            {**{key: value for key, value in vals.items() if not key.startswith(("recognition_","orientation_"))},
             "state": "pending", "recognition_state": "idle"} for vals in vals_list
        ])
        records._inspect_files()
        extra_values = [
            {"order_id":record.order_id.id,"name":file["name"],
             "file_data":{"filename":file["name"],"content":file["content"]}}
            for record,files in zip(records,batches) for file in files
        ]
        if extra_values:
            self.create(extra_values)
        return records

    def write(self, vals):
        if any(key.startswith(("recognition_","orientation_")) for key in vals):
            raise AccessError("识别结果由后台任务保存。")
        self._check_orders_editable(self.order_id)
        if "order_id" in vals:
            self._check_orders_editable(self.env["sale.order"].browse(vals["order_id"]))
        selection=vals.get("accessory_selection_data")
        if isinstance(selection,dict) and not selection.get("candidate_id") and "active_recognition_id" not in vals:
            if len(self)>1:
                return all(record.write(vals) for record in self)
            if self.active_recognition_id:
                vals={**vals,"accessory_selection_data":{**selection,"candidate_id":self.active_recognition_id.id}}
        if len(self) > 1 and set(vals).intersection(self._selection_field_names()):
            return all(record.write(vals) for record in self)
        vals = self._prepare_cabinet_values(self._prepare_product_series_values(vals))
        if "file_data" in vals or "name" in vals:
            vals = dict(vals, dimension_assist_data=False, accessory_selection_data=False)
        if any(key in vals for key in (
            "name", "file_data", "order_id", "order_line_id", "product_series", "product_template_id", "quantity", "width_mm", "depth_mm",
            "height_mm", "material", "thickness_mm", "coating_type", "display_color", "door_type", "selection_notes", "accessory_selection_data", "width_expression", "cabinet_configuration",
        )):
            vals = dict(vals, state="pending", reviewed_by=False, reviewed_at=False)
        if vals.get("state") == "confirmed":
            vals = dict(vals, reviewed_by=self.env.user.id, reviewed_at=fields.Datetime.now())
        result = super().write(vals)
        if "file_data" in vals or "name" in vals:
            self._inspect_files()
        elif "active_recognition_id" not in vals and any(key in vals for key in self._selection_field_names()):
            self._save_candidate_selection()
        elif vals.get("state") == "confirmed":
            self._save_candidate_selection()
        return result

    @api.ondelete(at_uninstall=False)
    def _unlink_only_editable(self):
        self._check_orders_editable(self.order_id)

    def _inspect_files(self):
        from PIL import Image
        from PyPDF2 import PdfReader

        allowed = {".pdf", ".dwg", ".dxf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
        for drawing in self:
            extension = PurePath(drawing.name).suffix.lower()
            if extension not in allowed:
                raise ValidationError("请上传 PDF、DWG、DXF 或常用图片格式。")
            content = drawing.file_data.content
            if not content:
                raise ValidationError("图纸文件为空，请重新上传。")
            pages = 0
            text = ""
            if extension == ".pdf":
                try:
                    reader = PdfReader(BytesIO(content))
                    pages = len(reader.pages)
                    extracted = []
                    for page in reader.pages:
                        extracted.append(page.extract_text() or "")
                        if sum(map(len, extracted)) >= 120000:
                            break
                    text = "\n".join(extracted)[:120000]
                except Exception as error:
                    raise ValidationError("无法读取此 PDF，请检查文件是否损坏或已加密。") from error
            elif extension not in (".dwg", ".dxf"):
                try:
                    with Image.open(BytesIO(content)) as image:
                        pages = getattr(image, "n_frames", 1)
                        image.verify()
                except Exception as error:
                    raise ValidationError("无法读取此图片，请检查文件内容。") from error
            super(SaleOrderDrawing, drawing).write({
                "page_count": pages, "source_text": text, "state": "pending",
            })
            super(SaleOrderDrawing,drawing).write({
                "orientation_data":False,"orientation_state":"queued",
                "orientation_started_at":False,"orientation_token":uuid.uuid4().hex,
            })
            self.env.ref("sale.ir_cron_drawing_orientation")._trigger()
            drawing._queue_recognition()

    @api.constrains("order_line_id", "order_id")
    def _check_linked_order(self):
        for drawing in self:
            if drawing.order_line_id and drawing.order_line_id.order_id != drawing.order_id:
                raise ValidationError("关联明细必须属于当前报价单。")

    @api.constrains("state", "product_series", "product_template_id", "quantity", "width_mm", "depth_mm",
                    "height_mm", "thickness_mm", "material")
    def _check_confirmed_selection(self):
        for drawing in self.filtered(lambda record: record.state == "confirmed"):
            if not drawing.product_series or not math.isfinite(drawing.quantity) or drawing.quantity <= 0:
                raise ValidationError("请选择产品类型，并填写大于零的数量。")
            dimensions = [drawing.width_mm, drawing.depth_mm, drawing.height_mm, drawing.thickness_mm]
            if not drawing.material or any(not math.isfinite(value) or value <= 0 for value in dimensions):
                raise ValidationError("机箱机柜选型需要填写宽度、深度、高度、材质和料厚。")

    def action_open_selection(self):
        self.ensure_one()
        self.check_access("read")
        return {
            "name": "图纸与选型",
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "views": [(self.env.ref("sale.sale_order_drawing_selection_form").id, "form")],
            "target": "new",
            "context": {**self.env.context, "dialog_size": "extra-large"},
        }

    def action_confirm_selection(self):
        self.check_access("write")
        self.write({
            "state": "confirmed", "reviewed_by": self.env.user.id,
            "reviewed_at": fields.Datetime.now(),
        })
        for drawing in self:
            drawing._sync_selection_order_line()
        return {"type": "ir.actions.act_window_close"}

    def _sync_selection_order_line(self):
        self.ensure_one()
        candidate = self.active_recognition_id
        line = self.order_id._selection_line_for_candidate(candidate) if candidate else self.order_line_id
        product = self.product_template_id
        if not product:
            cabinet = self.env.ref("sale.jingneng_product_category_cabinet", raise_if_not_found=False)
            product = self.env["product.template"].search([
                ("jingneng_catalog_series", "=", self.product_series),
                ("categ_id", "=", cabinet.id), ("sale_ok", "=", True),
                ("company_id", "in", [False, self.company_id.id]),
            ], limit=1)
        if not product and self.product_series in ("JA", "JP"):
            product = self.env["product.template"].search([
                ("default_code", "=", "JN-DEMO-" + self.product_series), ("sale_ok", "=", True),
                ("company_id", "in", [False, self.company_id.id]),
            ], limit=1)
        if not product or not product.product_variant_id:
            raise UserError("此系列尚未关联销售产品，请先维护产品目录。")
        values = {name: self[name] for name in self._selection_field_names()
                  if name not in ("product_template_id", "dimension_assist_data")}
        if values.get("accessory_selection_data"):
            values["accessory_selection_data"] = {**values["accessory_selection_data"], "candidate_id": 0}
        result = self.order_id._save_selection_line({
            **values, "product_id": product.product_variant_id.id, "line_id": line.id,
        }, from_drawing=True)
        line = self.env["sale.order.line"].browse(result["line_id"])
        line.write({"selection_drawing_id": self.id, "selection_candidate_id": candidate.id})
        super(SaleOrderDrawing, self).write({"order_line_id": line.id})
        if candidate:
            candidate.sudo().write({"excluded_from_quote": False})

    @api.model
    def _selection_field_names(self):
        return (
            "product_series", "product_template_id", "quantity", "width_mm", "depth_mm", "height_mm",
            "material", "thickness_mm", "coating_type", "display_color", "door_type", "selection_notes", "dimension_assist_data", "accessory_selection_data",
            "width_expression", "cabinet_configuration",
        )

    def _prepare_cabinet_values(self, vals):
        keys = ("product_series", "width_mm", "width_expression", "cabinet_configuration", "door_type",
                "depth_mm", "height_mm", "material", "thickness_mm", "coating_type", "accessory_selection_data")
        if not set(vals).intersection(keys):
            return vals
        current = {name: self[name] for name in keys} if len(self) == 1 else {}
        if "width_mm" in vals and "width_expression" not in vals:
            if len((current.get("cabinet_configuration") or {}).get("cabinets", [])) > 1:
                if not math.isclose(float(vals["width_mm"] or 0), current.get("width_mm") or 0):
                    raise ValidationError("当前为并柜，请在宽度栏逐柜修改宽度，不能用一个总宽覆盖柜位。")
            else:
                current["width_expression"] = f"{float(vals['width_mm']):g}" if vals["width_mm"] else ""
        current.update({key: vals[key] for key in keys if key in vals})
        if "door_type" in vals and "cabinet_configuration" not in vals:
            config = dict(current.get("cabinet_configuration") or {})
            cabinets = [dict(c) for c in config.get("cabinets", [])]
            if len(cabinets) == 1:
                cabinets[0].update(door_type=vals["door_type"], door_manual=True)
                current["cabinet_configuration"] = {**config, "cabinets": cabinets}
        normalized = _normalize_cabinet_values(current)
        output = {**vals, **{name: normalized[name] for name in
                            ("width_mm", "width_expression", "cabinet_configuration", "door_type")}}
        if normalized.get("accessory_selection_data"):
            output["accessory_selection_data"] = normalized["accessory_selection_data"]
        return output

    @api.onchange("width_expression", "cabinet_configuration", "product_series", "depth_mm", "height_mm",
                  "material", "thickness_mm", "coating_type", "accessory_selection_data")
    def _onchange_cabinet_configuration(self):
        for drawing in self:
            drawing.update(drawing._prepare_cabinet_values({name: drawing[name] for name in
                ("width_expression", "cabinet_configuration", "product_series", "depth_mm", "height_mm",
                 "material", "thickness_mm", "coating_type", "accessory_selection_data")}))

    def _current_recognition_candidates(self):
        self.ensure_one()
        return self.recognition_candidate_ids.filtered(
            lambda candidate: candidate.recognition_token == self.recognition_token
        )

    @api.constrains("active_recognition_id", "recognition_token")
    def _check_active_recognition(self):
        for drawing in self:
            candidate = drawing.active_recognition_id
            if candidate and (candidate.drawing_id != drawing
                              or candidate.recognition_token != drawing.recognition_token):
                raise ValidationError("请选择当前图纸本次识别返回的项目。")

    def _save_candidate_selection(self):
        for drawing in self.filtered("active_recognition_id"):
            candidate = drawing.active_recognition_id
            values = {name: drawing[name] for name in drawing._selection_field_names()}
            values["product_template_id"] = drawing.product_template_id.id or False
            candidate.sudo().write({
                "review_values": values,
                "review_state": drawing.state,
                "reviewed_by": drawing.reviewed_by.id or False,
                "reviewed_at": drawing.reviewed_at or False,
            })
            if drawing.recognition_state in ("review", "done"):
                completed = all(item.review_state == "confirmed"
                                for item in drawing._current_recognition_candidates().filtered(lambda c: not c.excluded_from_quote))
                super(SaleOrderDrawing, drawing).write({
                    "recognition_state": "done" if completed else "review",
                })

    def action_apply_recognition(self):
        self.ensure_one()
        self.check_access("write")
        self._check_orders_editable(self.order_id)
        candidate = self.active_recognition_id
        self._check_active_recognition()
        if not candidate:
            raise UserError("请先选择一个识别项目。")
        values = dict.fromkeys(self._selection_field_names(), False)
        values.update(coating_type=self.coating_type,display_color=self.display_color)
        values.update(quantity=0, width_mm=0, depth_mm=0, height_mm=0, thickness_mm=0)
        if candidate.review_values:
            values.update(candidate.review_values)
            product = self.env["product.template"].browse(values.get("product_template_id")).exists()
            if not values.get("product_series") and product and not self._series_from_product(product):
                values["product_template_id"] = False
        else:
            proposed = candidate.suggested_values or {}
            values.update({name: proposed[name] for name in self._selection_field_names()
                           if name in proposed})
            code = proposed.get("product_code")
            if code:
                product = self.env["product.template"].search([("default_code", "=", code)], limit=2)
                if len(product) == 1:
                    values["product_template_id"] = product.id
            attachments = candidate.attachment_names or []
            if attachments:
                values["selection_notes"] = "附件建议（数量待核对）：" + "、".join(attachments)
        values.update(state="pending", reviewed_by=False, reviewed_at=False)
        self.write(values)
        return True

    def _queue_recognition(self):
        for drawing in self:
            super(SaleOrderDrawing, drawing).write({
                "selection_row_hidden": False,
                "recognition_state": "queued", "recognition_token": uuid.uuid4().hex,
                "recognition_started_at": False, "recognition_finished_at": False,
                "recognition_data": False, "recognition_error": False,
                "recognition_completed_pages": 0, "active_recognition_id": False,
                "accessory_selection_data":{**drawing.accessory_selection_data,"candidate_id":0} if drawing.accessory_selection_data else False,
            })
        if any(drawing.orientation_state=="ready" for drawing in self):
            self.env.ref("sale.ir_cron_drawing_recognition")._trigger()

    def action_recognize(self):
        self.check_access("write")
        self._check_orders_editable(self.order_id)
        self.filtered(lambda drawing: drawing.recognition_state not in ("queued", "processing"))._queue_recognition()
        return True

    def action_cancel_recognition(self):
        self.check_access("write")
        self._check_orders_editable(self.order_id)
        for drawing in self.filtered(lambda drawing: drawing.recognition_state in ("queued", "processing")):
            super(SaleOrderDrawing, drawing).write({
                "recognition_state": "idle", "recognition_token": uuid.uuid4().hex,
                "recognition_error": "已取消识别，可重新开始。",
                "active_recognition_id": False,
                "accessory_selection_data":{**drawing.accessory_selection_data,"candidate_id":0} if drawing.accessory_selection_data else False,
            })
        return True

    @api.model
    def _recognition_configuration(self):
        parameters = self.env["ir.config_parameter"].sudo()
        keys = ("client", "bundle", "dependencies", "pure_dependencies", "renderer", "converter", "workspace")
        settings = {key: parameters.get_str("sale.drawing_recognition." + key) or "" for key in keys}
        required = ("client", "bundle", "dependencies", "pure_dependencies", "workspace")
        if any(not settings[key] or not Path(settings[key]).exists() for key in required):
            raise UserError("识图运行库配置不完整，请检查原识图核心和转换组件。")
        return settings

    def _run_recognition_page(self, path, page_index, token, settings):
        self.ensure_one()
        import psutil

        payload = {**settings, "path": str(path), "page_index": page_index}
        process = subprocess.Popen(
            [sys.executable, "-c", _DRAWING_RECOGNITION_WORKER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        started = time.monotonic()
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        first = True
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(input=encoded if first else None, timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    first = False
                    self.env.cr.execute(
                        "SELECT recognition_token, recognition_state FROM sale_order_drawing WHERE id = %s",
                        (self.id,),
                    )
                    current = self.env.cr.fetchone()
                    # Odoo uses repeatable-read. Release the snapshot so cancellation is visible.
                    self.env.cr.commit()
                    if not current or current != (token, "processing"):
                        return None
                    if time.monotonic() - started > 180:
                        raise UserError("本页识别超过 3 分钟，请重试或检查原图。")
            if process.returncode:
                _logger.warning("Drawing recognition failed for drawing %s page %s: %s",
                                self.id, page_index + 1, stderr.decode("utf-8", errors="replace")[-2000:])
                raise UserError("本页识别未完成，请检查图纸或识图运行库后重试。")
            prefix = b"ODOO_RECOGNITION_RESULT="
            line = next((line for line in reversed(stdout.splitlines()) if line.startswith(prefix)), None)
            if line is None:
                raise UserError("识图程序未返回有效结果。")
            return json.loads(line[len(prefix):].decode("utf-8"))
        finally:
            if process.poll() is None:
                try:
                    children = psutil.Process(process.pid).children(recursive=True)
                    for child in reversed(children):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                finally:
                    process.kill()
                    process.communicate(timeout=10)

    def _save_recognition_page(self, output, token):
        self.ensure_one()
        self.invalidate_recordset()
        if not self.exists() or self.recognition_token != token or self.recognition_state != "processing":
            return False
        previous = self.recognition_data or {"pages": [], "errors": []}
        merged = {**output,
                  "pages": previous.get("pages", []) + output.get("pages", []),
                  "errors": previous.get("errors", []) + output.get("errors", [])}
        merged.pop("candidates", None)
        candidates = []
        for index, candidate in enumerate(output.get("candidates", []), 1):
            candidates.append({
                "drawing_id": self.id, "recognition_token": token,
                "name": "第 %s 页 · %s · %s" % (candidate["page"], index, candidate["name"]),
                "page_number": candidate["page"],
                "suggested_values": candidate["suggested"],
                "evidence_summary": candidate["summary"],
                "attachment_names": candidate["attachment_names"],
                "raw_result": candidate["raw"],
            })
        created = self.env["sale.order.drawing.candidate"].sudo().create(candidates)
        values = {
            "recognition_data": merged, "recognition_completed_pages": self.recognition_completed_pages + 1,
            "source_text": "\n\n".join(
                "第 %s 页\n%s" % (page.get("source_page_number", "?"),
                                  page.get("raw_text") or page.get("text") or "")
                for page in merged["pages"]
            ),
        }
        if not self.active_recognition_id and created:
            values["active_recognition_id"] = created[0].id
            selection=self.accessory_selection_data
            if selection and not selection.get("candidate_id"):
                values["accessory_selection_data"]={**selection,"candidate_id":created[0].id}
        super(SaleOrderDrawing, self).write(values)
        return True

    @api.model
    def _cron_recognize_drawings(self):
        # Claim one file at a time. Commits release the HTTP-side editing transaction.
        stale = self.search([
            ("recognition_state", "=", "processing"),
            ("recognition_started_at", "<", fields.Datetime.now() - timedelta(minutes=20)),
        ])
        if stale:
            super(SaleOrderDrawing, stale).write({
                "recognition_state": "failed", "recognition_error": "识别任务中断，请重新识别。",
            })
        self.env["ir.cron"]._commit_progress(remaining=self.search_count([("recognition_state", "=", "queued"),("orientation_state","=","ready")]))
        while True:
            self.env.cr.execute(
                "SELECT id FROM sale_order_drawing WHERE recognition_state = 'queued' AND orientation_state = 'ready' "
                "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
            )
            claimed = self.env.cr.fetchone()
            if not claimed:
                return
            drawing = self.browse(claimed[0])
            token, content, filename = drawing.recognition_token, drawing.file_data.content, drawing.name
            page_count = max(1, drawing.page_count)
            super(SaleOrderDrawing, drawing).write({
                "recognition_state": "processing", "recognition_started_at": fields.Datetime.now(),
            })
            self.env["ir.cron"]._commit_progress()
            try:
                settings = self._recognition_configuration()
                with TemporaryDirectory(prefix="drawing-", dir=settings["workspace"]) as folder:
                    path = Path(folder) / PurePath(filename).name
                    path.write_bytes(content)
                    for page_index in range(page_count):
                        try:
                            output = drawing._run_recognition_page(path, page_index, token, settings)
                        except (UserError, OSError, ValueError) as error:
                            output = {"pages": [], "errors": [{"page": page_index+1, "message": str(error)}]}
                        if output is None:
                            break
                        for attempt in range(3):
                            try:
                                saved = drawing._save_recognition_page(output, token)
                                break
                            except SerializationFailure:
                                self.env.cr.rollback()
                                if attempt == 2:
                                    raise
                        if not saved:
                            break
                        self.env["ir.cron"]._commit_progress()
                drawing.invalidate_recordset()
                if drawing.exists() and drawing.recognition_token == token and drawing.recognition_state == "processing":
                    candidates = drawing._current_recognition_candidates()
                    errors = (drawing.recognition_data or {}).get("errors", [])
                    super(SaleOrderDrawing, drawing).write({
                        "recognition_state": "review" if candidates else "failed",
                        "recognition_finished_at": fields.Datetime.now(),
                        "recognition_error": "\n".join(
                            "第 %s 页：%s" % (item["page"], item["message"]) for item in errors
                        ) or False,
                    })
            except Exception:
                self.env.cr.rollback()
                _logger.exception("Drawing recognition task failed for drawing %s", drawing.id)
                if drawing.exists() and drawing.recognition_token == token:
                    super(SaleOrderDrawing, drawing).write({
                        "recognition_state": "failed",
                        "recognition_error": "识别任务未完成，请检查运行库后重试。",
                        "recognition_finished_at": fields.Datetime.now(),
                    })
            if not self.env["ir.cron"]._commit_progress(1):
                return


class SaleOrderDrawingCandidate(models.Model):
    _name = "sale.order.drawing.candidate"
    _description = "图纸识别项目与复核证据"
    _order = "page_number, id"

    drawing_id = fields.Many2one("sale.order.drawing", required=True, ondelete="cascade", index=True)
    name = fields.Char(required=True)
    page_number = fields.Integer(string="页码", required=True)
    recognition_token = fields.Char(required=True, index=True)
    suggested_values = fields.Json(readonly=True)
    evidence_summary = fields.Text(readonly=True)
    attachment_names = fields.Json(readonly=True)
    raw_result = fields.Json(readonly=True)
    review_values = fields.Json(readonly=True)
    review_state = fields.Selection([("pending", "待复核"), ("confirmed", "已复核")], default="pending", readonly=True)
    reviewed_by = fields.Many2one("res.users", readonly=True)
    reviewed_at = fields.Datetime(readonly=True)
    excluded_from_quote = fields.Boolean(default=False)
