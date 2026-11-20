from pathlib import Path
from runpy import run_path
import json
stage=Path(__file__).parent
root=Path('C:/Users/Administrator/AppData/Local/OdooNativePreview')
source=root/'runtime/server/odoo/addons/sale'
target=run_path('G:/京能/Odoo/京能板件演示/私有导入/切换原安装版演示库.py')['Installation'](root,'odoo_customer_demo',(8069,15432))
files=['static/src/js/dashboard/dashboard.js','static/src/js/dashboard/dashboard.xml','static/src/js/dashboard/dashboard.scss']
installed=json.loads((stage/'installed.json').read_text(encoding='utf-8')) if (stage/'installed.json').exists() else {name:(source/name).read_text(encoding='utf-8') for name in files}
for name in files:assert (source/name).read_text(encoding='utf-8')==installed[name], 'Concurrent edit: '+name
for name in files:(source/name).write_bytes((stage/name).read_bytes())
installed={name:(source/name).read_text(encoding='utf-8') for name in files}
(stage/'installed.json').write_text(json.dumps(installed,ensure_ascii=False),encoding='utf-8')
target.stop_web();target.restart_web()
print('Original asset service restarted',flush=True)
