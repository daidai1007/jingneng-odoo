from pathlib import Path
from runpy import run_path

root=Path('C:/Users/Administrator/AppData/Local/OdooNativePreview')
target=run_path('G:/京能/Odoo/京能板件演示/私有导入/切换原安装版演示库.py')['Installation'](root,'odoo_customer_demo',(8069,15432))
code='''
from odoo import Command, fields
from datetime import datetime
import json
results=[]
try:
    company=env.company
    User=env['res.users'].with_context(no_reset_password=True)
    limited=User.create({'name':'REPORT transaction ACL test','login':'report-acl-test-'+str(datetime.now().timestamp()),
        'company_id':company.id,'company_ids':[Command.set([company.id])],
        'group_ids':[Command.set([env.ref('base.group_user').id,env.ref('sales_team.group_sale_salesman').id])]})
    partner=env['sale.order'].search([],limit=1).partner_id
    def quote(label,extra=None):
        vals={'name':'REPORT-TRANSACTION-'+label,'partner_id':partner.id,'state':'sent','quotation_workflow_step':'quotation',
              'quotation_first_sent_at':'2026-11-03 01:00:00','user_id':env.user.id}
        vals.update(extra or {})
        return env['sale.order'].create(vals)
    own=quote('OWN',{'user_id':limited.id})
    other=quote('OTHER')
    visible=env['sale.order'].with_user(limited).search([('id','in',[own.id,other.id])]).ids
    assert own.id in visible and other.id not in visible, 'Fixture record rules not configured'
    data=env['sale.report'].with_user(limited).get_jingneng_report_data({'period':'1','analysis':'bids'})
    assert own.id in [r['id'] for r in data['rows']] and other.id not in [r['id'] for r in data['rows']], 'Report bypasses order rules'
    results.append('普通销售人员仅统计自己有权读取的订单')
    currency=env['res.currency'].search([('id','!=',company.currency_id.id)],limit=1)
    pricelist=env['product.pricelist'].create({'name':'REPORT transaction FX test','currency_id':currency.id,'company_id':company.id})
    foreign=quote('FOREIGN',{'pricelist_id':pricelist.id})
    assert foreign.currency_id==currency
    data=env['sale.report'].get_jingneng_report_data({'period':'1','analysis':'bids','currency':currency.id})
    assert foreign.id in [r['id'] for r in data['rows']] and all(env['sale.order'].browse(r['id']).currency_id==currency for r in data['rows'])
    data=env['sale.report'].get_jingneng_report_data({'period':'1','analysis':'bids'})
    assert foreign.id not in [r['id'] for r in data['rows']]
    results.append('多币种分别统计，未合并不同币种金额')
    print('REPORT_SECURITY_CHECKS '+json.dumps(results,ensure_ascii=False),flush=True)
finally:
    env.cr.rollback()
'''
output=target.command(target.python,[target.runtime/'server/odoo-bin','shell','-c',root/'odoo.conf','-d','odoo_customer_demo','--no-http','--log-level=warn'],code.encode('utf-8'),timeout=120)
lines=[x for x in output.splitlines() if x.startswith('REPORT_')]
if not lines:raise RuntimeError(output[-2000:])
print('\n'.join(lines))
(Path(__file__).parent/'security-result.txt').write_text('\n'.join(lines),encoding='utf-8')
