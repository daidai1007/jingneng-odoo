# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models
from odoo.fields import Domain
from odoo.models import TableSQL
from odoo.tools import SQL, clean_context

from odoo.addons.sale.models.sale_order import SALE_ORDER_STATE


class SaleReport(models.Model):
    _name = "sale.report"
    _description = "Sales Analysis Report"
    _auto = False
    _rec_name = "date"
    _order = "date desc"

    @api.model
    def _get_done_states(self):
        return ["sale"]

    @api.model
    def _selection_target_model(self):
        return [
            (model.model, model.name)
            for model
            in self.env['ir.model'].sudo().search_fetch(
                [('transient', '=', False)], ['model', 'name']
            )
            if model.model in self.env
        ]

    # sale.order fields
    name = fields.Char(string="Order Reference", readonly=True)
    line_name = fields.Char(string="Order Line Name", readonly=True)
    date = fields.Datetime(string="Order Date", readonly=True)
    partner_id = fields.Many2one(comodel_name="res.partner", string="Customer", readonly=True)
    company_id = fields.Many2one(comodel_name="res.company", readonly=True)
    pricelist_id = fields.Many2one(comodel_name="product.pricelist", readonly=True)
    team_id = fields.Many2one(comodel_name="crm.team", string="Sales Team", readonly=True)
    user_id = fields.Many2one(comodel_name="res.users", string="Salesperson", readonly=True)
    state = fields.Selection(selection=SALE_ORDER_STATE, string="Status", readonly=True)
    invoice_status = fields.Selection(
        selection=[
            ("upselling", "Upselling Opportunity"),
            ("invoiced", "Fully Invoiced"),
            ("to invoice", "To Invoice"),
            ("no", "Nothing to Invoice"),
        ],
        string="Order Invoice Status",
        readonly=True,
    )

    campaign_id = fields.Many2one(comodel_name="utm.campaign", string="Campaign", readonly=True)
    medium_id = fields.Many2one(comodel_name="utm.medium", string="Medium", readonly=True)
    source_id = fields.Many2one(comodel_name="utm.source", string="Source", readonly=True)
    utm_reference = fields.Reference(string="UTM Reference", selection="_selection_target_model")

    # res.partner fields
    commercial_partner_id = fields.Many2one(
        comodel_name="res.partner", string="Customer Entity", readonly=True
    )
    country_id = fields.Many2one(
        comodel_name="res.country", string="Customer Country", readonly=True
    )
    industry_id = fields.Many2one(
        comodel_name="res.partner.industry", string="Customer Industry", readonly=True
    )
    partner_zip = fields.Char(string="Customer ZIP", readonly=True)
    state_id = fields.Many2one(
        comodel_name="res.country.state", string="Customer State", readonly=True
    )
    partner_tag_ids = fields.Many2many(
        string="Customer Tags", related="partner_id.category_id", readonly=True
    )

    # sale.order.line fields
    order_reference = fields.Reference(
        string="Order", selection=[("sale.order", "Sales Order")], aggregator="count_distinct"
    )

    categ_id = fields.Many2one(
        comodel_name="product.category", string="Product Category", readonly=True
    )
    product_id = fields.Many2one(
        comodel_name="product.product", string="Product Variant", readonly=True
    )
    product_tmpl_id = fields.Many2one(
        comodel_name="product.template", string="Product", readonly=True
    )
    product_uom_id = fields.Many2one(comodel_name="uom.uom", string="Unit", readonly=True)
    product_uom_qty = fields.Float(string="Qty Ordered", readonly=True)
    qty_to_deliver = fields.Float(string="Qty To Deliver", readonly=True)
    qty_delivered = fields.Float(string="Qty Delivered", readonly=True)
    qty_to_invoice = fields.Float(string="Qty To Invoice", readonly=True)
    qty_invoiced = fields.Float(string="Qty Invoiced", readonly=True)
    price_subtotal = fields.Monetary(string="Untaxed Total", readonly=True)
    price_total = fields.Monetary(string="Total", readonly=True)
    untaxed_amount_to_invoice = fields.Monetary(string="Untaxed Amount To Invoice", readonly=True)
    untaxed_amount_invoiced = fields.Monetary(string="Untaxed Amount Invoiced", readonly=True)
    untaxed_delivered_amount = fields.Monetary(string="Untaxed Amount Delivered", readonly=True)
    line_invoice_status = fields.Selection(
        selection=[
            ("upselling", "Upselling Opportunity"),
            ("invoiced", "Fully Invoiced"),
            ("to invoice", "To Invoice"),
            ("no", "Nothing to Invoice"),
        ],
        string="Invoice Status",
        readonly=True,
    )

    weight = fields.Float(string="Gross Weight", readonly=True)
    volume = fields.Float(string="Volume", readonly=True)
    price_unit = fields.Monetary(string="Unit Price", aggregator="avg", readonly=True)
    discount = fields.Float(string="Discount %", readonly=True, aggregator="avg")
    discount_amount = fields.Monetary(string="Discount Amount", readonly=True)

    # aggregates or computed fields
    nbr = fields.Integer(string="# of Lines", readonly=True)
    currency_id = fields.Many2one(comodel_name="res.currency", readonly=True)

    @property
    def _table_sql(self) -> SQL:
        today = fields.Date.today()
        query = self.env['sale.order.line'].sudo().with_context(date_to=today)._search(self._order_line_domain())
        query.groupby = SQL(", ").join(self._groupby_list(query.table))
        return query.subselect(*self._select_dict_to_list(self._select_dict(query.table)))

    def _order_line_domain(self):
        return Domain('display_type', '=', False)

    def _select_dict_to_list(self, select_dict):
        return [SQL("%s AS %s", select_dict.get(fname, SQL("NULL")), SQL.identifier(fname)) for fname, field in self._fields.items() if field.store]

    def _select_dict(self, table: TableSQL):
        uom_ratio = SQL("(COALESCE(%s, 1) / NULLIF(COALESCE(%s, 1), 0.0))", table.product_uom_id.factor, table.product_id.uom_id.factor)
        order_rate = self._case_value_or_one(table.order_id.currency_rate)
        rate = SQL("%s / %s", table.consolidation_rate, order_rate)
        return {
            'id': SQL("MIN(%s)", table.id),
            'product_id': table.product_id,
            'line_name': table.name,
            'line_invoice_status': SQL("%s", table.invoice_status),
            'product_uom_id': SQL("CASE WHEN %s IS NULL THEN %s ELSE %s END", table.product_id, table.product_id.uom_id, table.product_uom_id),
            'product_uom_qty': SQL("CASE WHEN %s IS NOT TRUE THEN SUM(%s * %s) ELSE 0 END", table.is_downpayment, table.product_uom_qty, uom_ratio),
            'qty_delivered': SQL("CASE WHEN %s IS NOT TRUE THEN SUM(%s * %s) ELSE 0 END", table.is_downpayment, table.qty_delivered, uom_ratio),
            'qty_to_deliver': SQL("CASE WHEN %s IS NOT TRUE THEN SUM((%s - %s) * %s) ELSE 0 END", table.is_downpayment, table.product_uom_qty, table.qty_delivered, uom_ratio),
            'qty_invoiced': SQL("CASE WHEN %s IS NOT TRUE THEN SUM(%s * %s) ELSE 0 END", table.is_downpayment, table.qty_invoiced, uom_ratio),
            'qty_to_invoice': SQL("CASE WHEN %s IS NOT TRUE THEN SUM(%s * %s) ELSE 0 END", table.is_downpayment, table.qty_to_invoice, uom_ratio),
            'price_unit': SQL("CASE WHEN %s IS NOT TRUE THEN AVG(%s * %s) ELSE 0 END", table.is_downpayment, table.price_unit, rate),
            'price_total': SQL("CASE WHEN %s IS NOT TRUE THEN SUM(%s * %s) ELSE 0 END", table.is_downpayment, table.price_total, rate),
            'price_subtotal': SQL("CASE WHEN %s IS NOT TRUE THEN SUM(%s * %s) ELSE 0 END", table.is_downpayment, table.price_subtotal, rate),
            'untaxed_amount_to_invoice': SQL("SUM(%s * %s)", table.untaxed_amount_to_invoice, rate),
            'untaxed_amount_invoiced': SQL("SUM(%s * %s)", table.untaxed_amount_invoiced, rate),
            'untaxed_delivered_amount': SQL("SUM(%s * %s * %s)", table.price_unit, table.qty_delivered, rate),
            'nbr': SQL("COUNT(*)"),
            'name': table.order_id.name,
            'date': SQL("%s", table.order_id.date_order),
            'state': table.order_id.state,
            'invoice_status': table.order_id.invoice_status,
            'partner_id': table.order_id.partner_id,
            'user_id': table.order_id.user_id,
            'company_id': table.order_id.company_id,
            'campaign_id': table.order_id.campaign_id,
            'medium_id': table.order_id.medium_id,
            'source_id': table.order_id.source_id,
            'utm_reference': table.order_id.utm_reference,
            'categ_id': table.product_id.categ_id,
            'pricelist_id': table.order_id.pricelist_id,
            'team_id': table.order_id.team_id,
            'product_tmpl_id': table.product_id.product_tmpl_id,
            'commercial_partner_id': table.order_id.partner_id.commercial_partner_id,
            'country_id': table.order_id.partner_id.country_id,
            'industry_id': table.order_id.partner_id.industry_id,
            'state_id': table.order_id.partner_id.state_id,
            'partner_zip': SQL("%s", table.order_id.partner_id.zip),
            'weight': SQL("SUM(%s * %s * %s)", table.product_id.weight, table.product_uom_qty, uom_ratio),
            'volume': SQL("SUM(%s * %s * %s)", table.product_id.volume, table.product_uom_qty, uom_ratio),
            'discount': table.discount,
            'discount_amount': SQL("SUM(%s * %s * %s / 100.0 * %s)", table.price_unit, table.product_uom_qty, table.discount, rate),
            'currency_id': SQL("%s", self.env.company.currency_id.id),
            'order_reference': SQL("concat('sale.order', ',', %s)", table.order_id),
        }

    def _groupby_list(self, table: TableSQL):
        groupby_list = [
            table.product_id,
            table.price_unit,
            table.invoice_status,
            table.name,
            table.product_uom_id,
            table.is_downpayment,
            table.discount,
            table.order_id,
            table.order_id.id,
            table.product_id.uom_id,
            table.product_id.product_tmpl_id,
            table.product_id.product_tmpl_id.id,
            table.order_id.partner_id.id,
        ]
        if table.consolidation_rate != SQL('1'):
            groupby_list.append(table.consolidation_rate)
        return groupby_list

    def _case_value_or_one(self, value):
        return SQL("CASE COALESCE(%(value)s, 0) WHEN 0 THEN 1.0 ELSE %(value)s END", value=value)

    def _get_order_reference(self):
        """Resolve the order from the line id, without reading the view."""
        self.ensure_one()
        line = self.env["sale.order.line"].browse(self.id)
        line.fetch(["order_id"])
        return line.order_id

    @api.readonly
    def action_open_order(self):

        order = self._get_order_reference()
        return order._get_records_action(context=clean_context(self.env.context))


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
