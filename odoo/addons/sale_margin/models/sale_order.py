# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    margin = fields.Monetary("Margin", compute="_compute_margin", store=True, groups="base.group_user")
    margin_percent = fields.Float(
        "Margin (%)", compute="_compute_margin", store=True, aggregator="avg", groups="base.group_user"
    )

    @api.depends("order_line.margin", "amount_untaxed")
    def _compute_margin(self):
        if not all(self._ids):
            for order in self:
                order.margin = sum(order.order_line.mapped("margin"))
                order.margin_percent = order.amount_untaxed and order.margin / order.amount_untaxed
        else:
            # On batch records recomputation (e.g. at install), compute the margins
            # with a single read_group query for better performance.
            # This isn't done in an onchange environment because (part of) the data
            # may not be stored in database (new records or unsaved modifications).
            grouped_order_lines_data = self.env["sale.order.line"]._read_group(
                [("order_id", "in", self.ids)], ["order_id"], ["margin:sum"]
            )
            mapped_data = {order.id: margin for order, margin in grouped_order_lines_data}
            for order in self:
                order.margin = mapped_data.get(order.id, 0.0)
                order.margin_percent = order.amount_untaxed and order.margin / order.amount_untaxed

    @api.depends("margin", "order_line.saved_result_snapshot", "order_line.selection_pricing_result")
    def _compute_extra_total_fields(self):
        super()._compute_extra_total_fields()

        for order in self:
            # Saved quote margins are shown as M/N on the quote row. The
            # native purchase-price margin is a different ledger and must not
            # appear as a second, contradictory total for imported snapshots.
            if not self.env.user.has_group("base.group_user"):
                continue
            if not order.margin or any(order.order_line.mapped("saved_result_snapshot")) or any(order.order_line.mapped("selection_pricing_result")):
                continue

            groups = order.extra_total_fields
            for basic_group in filter(lambda group: group.get("name") == "basic", groups):
                basic_group["lines"].append({
                    "label": self.env._(
                        "Margin (%(percent).0f%%)", percent=order.margin_percent * 100
                    ),
                    "value": order.margin,
                })
                order.extra_total_fields = groups
