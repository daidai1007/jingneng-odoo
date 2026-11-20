from pathlib import Path
import ast
from lxml import etree

stage = Path(__file__).parent
source = Path('C:/Users/Administrator/AppData/Local/OdooNativePreview/runtime/server/odoo/addons/sale')
files = ['models/sale_order.py','report/sale_report.py','views/sale_order_views.xml','report/sale_report_views.xml','views/sale_menus.xml','static/src/js/dashboard/dashboard.js','static/src/js/dashboard/dashboard.xml','static/src/js/dashboard/dashboard.scss']
for name in files:
    path = stage/'before'/name
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists(): path.write_bytes((source/name).read_bytes())
    target = stage/name
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(path.read_bytes())

def change(name, fn):
    path = stage/name
    path.write_text(fn(path.read_text(encoding='utf-8')),encoding='utf-8',newline='\n')

feedback_fields = '''
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

'''

def edit_order(s):
    anchor = '    quotation_source_date = fields.Date('
    assert anchor in s
    s = s.replace(anchor, feedback_fields + anchor, 1)
    create_anchor = '    def create(self, vals_list):\n        for vals in vals_list:\n'
    assert create_anchor in s
    s = s.replace(create_anchor, create_anchor + '''            if vals.get('state') == 'sent':
                vals.setdefault('quotation_sent_at', fields.Datetime.now())
                vals.setdefault('quotation_first_sent_at', vals['quotation_sent_at'])
            if vals.get('quotation_result') in ('won', 'lost'):
                vals.setdefault('quotation_feedback_date', fields.Date.context_today(self))
''',1)
    start = s.index('    def write(self, vals):', s.index('class SaleOrder('))
    end = s.index('    # === ACTION METHODS ===#',start)
    s = s[:start] + '''    def write(self, vals):
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

''' + s[end:]
    return s
change('models/sale_order.py',edit_order)

report_method = '''
    @api.model
    def get_jingneng_report_data(self, filters=None):
        """Authorized canonical orders; every presentation aggregates this same cohort.

        Never infer a bid outcome from an order state. Revision links are explicit;
        cancelled/draft revisions do not replace an effective sent quotation.
        """
        from datetime import datetime, time
        from dateutil.relativedelta import relativedelta
        import pytz
        from odoo.exceptions import UserError

        filters = filters or {}
        analysis = filters.get('analysis', 'bids')
        if analysis not in ('bids', 'sales'):
            raise UserError('请选择有效的报表。')
        today = fields.Date.context_today(self)
        period = str(filters.get('period', '6'))
        if period == 'custom':
            try:
                start = fields.Date.to_date(filters.get('start'))
                end = fields.Date.to_date(filters.get('end'))
            except (ValueError, TypeError):
                raise UserError('请填写有效的起止日期。')
            if not start or not end or start > end:
                raise UserError('开始日期不能晚于结束日期。')
        else:
            if period not in ('1','3','6','12'):
                raise UserError('请选择有效的日期范围。')
            start = today.replace(day=1) - relativedelta(months=int(period)-1)
            end = today
        if (end-start).days > 3660:
            raise UserError('单次查询最多十年，请缩小日期范围。')
        tz = pytz.timezone(self.env.context.get('tz') or self.env.user.tz or 'UTC')
        def utc(day):
            return tz.localize(datetime.combine(day,time.min)).astimezone(pytz.UTC).replace(tzinfo=None)
        def day(value):
            return fields.Datetime.context_timestamp(self,value).date().isoformat() if value else False

        Order = self.env['sale.order']
        # No sudo: ordinary order ACLs and multi-company record rules apply.
        domain = [('company_id','in',self.env.companies.ids),('state','!=','cancel')]
        if analysis == 'bids':
            domain += [('quotation_first_sent_at','!=',False),('quotation_status','!=','draft')]
        else:
            scope = filters.get('scope', 'sale')
            if scope not in ('sale','contract','quote','valid'):
                raise UserError('请选择有效的统计状态。')
            date_field = 'date_order'
            if scope == 'sale':
                domain += [('state','=','sale')]
            elif scope == 'contract':
                domain += [('quotation_status','=','contract'),('quotation_contract_at','!=',False)]
                date_field = 'quotation_contract_at'
            elif scope == 'quote':
                domain += [('quotation_status','in',['quotation','sent'])]
                date_field = 'create_date'
            domain += [(date_field,'>=',utc(start)),(date_field,'<',utc(end+relativedelta(days=1)))]
        if Order.search_count(domain,limit=20001) > 20000:
            raise UserError('数据超过两万份，请缩小查询范围后重试。')
        orders = Order.search(domain,order='id')
        if analysis == 'bids':
            # Resolve parents in batches through the same ACLs; never expose hidden ancestors.
            known = {o.id:o for o in orders}
            frontier = orders
            while frontier:
                ids = set(frontier.mapped('quotation_revision_of_id').ids) - set(known)
                frontier = Order.search([('id','in',list(ids))]) if ids else Order.browse()
                known.update({o.id:o for o in frontier})
            groups = {}
            for order in orders:
                current = order
                depth = 0
                seen = set()
                first = order.quotation_first_sent_at
                while current.quotation_revision_of_id.id in known and current.id not in seen:
                    seen.add(current.id)
                    current = known[current.quotation_revision_of_id.id]
                    depth += 1
                    if current.quotation_first_sent_at:
                        first = min(first,current.quotation_first_sent_at)
                key = current.id
                candidate = (depth,order.quotation_first_sent_at,order.id)
                if key not in groups or candidate > groups[key][0]:
                    groups[key] = (candidate,order,first,depth+1)
            cohort = [(o,first,version) for _,o,first,version in groups.values() if start.isoformat() <= day(first) <= end.isoformat()]
        else:
            cohort = [(o,o[date_field],1) for o in orders]
        currency_records = self.env['res.currency'].browse(list({o.currency_id.id for o,_,_ in cohort} | {self.env.company.currency_id.id}))
        currencies = [{'id':c.id,'name':c.name,'symbol':c.symbol,'digits':c.decimal_places} for c in currency_records]
        currency_id = int(filters.get('currency') or self.env.company.currency_id.id)
        if currency_id not in currency_records.ids:
            currency_id = self.env.company.currency_id.id
        reasons = dict(Order._fields['quotation_lost_reason']._description_selection(self.env))
        rows = []
        for order,first,version in cohort:
            if order.currency_id.id != currency_id:
                continue
            lines = []
            for line in order.order_line.filtered(lambda l: not l.display_type and not l.is_downpayment):
                names = []
                category = line.product_id.categ_id
                while category:
                    names.append(category.name)
                    category = category.parent_id
                kind = '附件' if '附件' in names else ('机箱机柜' if '机箱机柜' in names else '未分类')
                lines.append({'product':line.product_id.id or 0, 'name':line.product_id.display_name or line.name,
                    'kind':kind,'unit_id':line.product_uom_id.id,'unit':line.product_uom_id.name or '未指定单位',
                    'qty':line.product_uom_qty,'amount':line.price_total})
            rows.append({'id':order.id,'name':order.name,'version':version,
                'customer_id':order.partner_id.id,'customer':order.partner_id.display_name,
                'person_id':order.user_id.id or 0,'person':order.user_id.name or '未分配',
                'date':day(first),'amount':order.amount_total,'result':order.quotation_result or 'pending',
                'feedback':str(order.quotation_feedback_date or ''),'reason':order.quotation_lost_reason or 'unrecorded',
                'notes':order.quotation_feedback_notes or '', 'followup':str(order.quotation_followup_date or ''),
                'lines':lines})
        missing_domain = [('company_id','in',self.env.companies.ids),('state','in',['draft','sent','sale']),
            ('quotation_first_sent_at','=',False)]
        return {'rows':rows,'start':str(start),'end':str(end),'today':str(today),'currency':currency_id,
            'currencies':currencies,'reasons':reasons,
            'missing_sent':Order.search_count(missing_domain) if analysis == 'bids' else 0,
            'unclassified':sum(any(l['kind']=='未分类' for l in r['lines']) for r in rows)}
'''
change('report/sale_report.py',lambda s:s+'\n'+report_method)

feedback_view = '''
    <record id="sale_order_customer_feedback_form" model="ir.ui.view">
        <field name="name">sale.order.customer.feedback</field><field name="model">sale.order</field>
        <field name="priority">10000</field>
        <field name="arch" type="xml">
            <form string="客户反馈" create="false" delete="false">
                <div class="alert alert-info" role="note">按客户实际反馈登记结果。首次正式发送日期用于统计归属；合同、订单和取消状态不会自动判定中标或失标。</div>
                <group>
                    <group><field name="name" readonly="1"/><field name="partner_id" readonly="1"/>
                        <field name="quotation_first_sent_at" readonly="1"/><field name="quotation_revision_of_id" domain="[('id','!=',id),('partner_id','=',partner_id),('company_id','=',company_id)]" options="{'no_create': True}"/>
                    </group>
                    <group><field name="quotation_result" widget="radio"/>
                        <field name="quotation_feedback_date" invisible="quotation_result == 'pending'"/>
                        <field name="quotation_lost_reason" invisible="quotation_result != 'lost'"/>
                        <field name="quotation_followup_date" invisible="quotation_result != 'pending'"/>
                    </group>
                </group>
                <field name="company_id" invisible="1"/>
                <field name="quotation_feedback_notes" placeholder="填写客户反馈或待反馈的跟进安排"/>
                <footer><button string="保存" special="save" class="btn-primary"/><button string="取消" special="cancel" class="btn-secondary"/></footer>
            </form>
        </field>
    </record>
'''
def edit_form(s):
    s = s.replace('<button name="action_mark_quotation"', '<field name="quotation_first_sent_at" invisible="1"/><button name="action_open_customer_feedback" type="object" string="登记客户反馈" invisible="not quotation_first_sent_at"/>\n                <button name="action_mark_quotation"',1)
    list_start=s.index('<record id="sale_order_tree"')
    list_end=s.index('</record>',list_start)
    sub=s[list_start:list_end]
    anchor='<field name="quotation_status" widget="badge"'
    badge='''<field name="quotation_first_sent_at" column_invisible="True"/>
                <field name="quotation_result" string="报价结果" readonly="1" widget="badge" optional="show"
                       invisible="not quotation_first_sent_at"
                       decoration-success="quotation_result == 'won'" decoration-danger="quotation_result == 'lost'"
                       decoration-info="quotation_result == 'pending'"/>
                '''
    assert anchor in sub
    s=s[:list_start]+sub.replace(anchor,badge+anchor,1)+s[list_end:]
    title='''                </div>
                <group name="sale_header">'''
    s=s.replace(title,'''                    <div class="d-flex align-items-center gap-2 mt-2" invisible="not quotation_first_sent_at">
                        <span class="text-muted">报价结果</span>
                        <field name="quotation_result" readonly="1" widget="badge"
                               decoration-success="quotation_result == 'won'" decoration-danger="quotation_result == 'lost'"
                               decoration-info="quotation_result == 'pending'"/>
                    </div>
                </div>
                <group name="sale_header">''',1)
    search_start=s.index('<record id="view_sales_order_filter"')
    search_end=s.index('</record>',search_start)
    sub=s[search_start:search_end]
    sub=sub.replace('</search>', '''<separator/>
                <filter name="quotation_feedback_won" string="中标" domain="[('quotation_first_sent_at','!=',False),('quotation_result','=','won')]"/>
                <filter name="quotation_feedback_lost" string="失标" domain="[('quotation_first_sent_at','!=',False),('quotation_result','=','lost')]"/>
                <filter name="quotation_feedback_pending" string="待反馈" domain="[('quotation_first_sent_at','!=',False),('quotation_result','=','pending')]"/>
            </search>''')
    s=s[:search_start]+sub+s[search_end:]
    return s.replace('</odoo>',feedback_view+'\n</odoo>')
change('views/sale_order_views.xml',edit_form)
actions = []
for key,label in [('sales','销售'),('people','销售人员'),('products','产品'),('customers','客户')]:
    actions.append(f'''    <record id="action_jingneng_report_{key}" model="ir.actions.client">
        <field name="name">{label}分析</field><field name="tag">sale.jingneng_reports</field>
        <field name="path">jingneng-reports-{key}</field><field name="params" eval="{{'dimension': '{key}'}}"/>
    </record>''')
change('report/sale_report_views.xml',lambda s:s.replace('</odoo>','\n'.join(actions)+'\n</odoo>'))
def edit_menu(s):
    for old,new in [('all','sales'),('salesperson','people'),('products','products'),('customers','customers')]:
        s = s.replace(f'action="action_order_report_{old}"',f'action="action_jingneng_report_{new}"')
    return s
change('views/sale_menus.xml',edit_menu)

change('static/src/js/dashboard/dashboard.js',lambda s:s+'\n'+(stage/'frontend.js').read_text(encoding='utf-8'))
change('static/src/js/dashboard/dashboard.xml',lambda s:s.replace('</templates>',(stage/'frontend.xml').read_text(encoding='utf-8')+'\n</templates>'))
change('static/src/js/dashboard/dashboard.scss',lambda s:s+'\n'+(stage/'frontend.scss').read_text(encoding='utf-8'))

for name in files:
    if name.endswith('.py'):ast.parse((stage/name).read_text(encoding='utf-8'),filename=name)
    if name.endswith('.xml'):etree.parse(str(stage/name))
print('Backend and XML staged')
