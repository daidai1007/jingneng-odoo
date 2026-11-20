import { browser } from "@web/core/browser/browser";
import { Component, onMounted, onPatched, onWillUnmount, proxy, signal, t, useProps } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { DropdownItem } from "@web/core/dropdown/dropdown_item";
import { Dialog } from "@web/core/dialog/dialog";
import { x2ManyCommands } from "@web/core/orm_plugin";
import { getFieldsSpec } from "@web/model/relational_model/utils";
import { useSubEnv } from "@web/owl2/utils";
import {
    ProductLabelSectionAndNoteListRender,
    productLabelSectionAndNoteOne2Many,
    ProductLabelSectionAndNoteOne2Many,
} from "@account/components/product_label_section_and_note_o2m/product_label_section_and_note_field_o2m";
import { sectionAndNoteFieldOne2Many } from "@account/components/section_and_note_fields_backend/section_and_note_fields_backend";
import { openComboConfigurator } from "@sale/js/combo_configurator_utils";
import { clearSelectedComboItems, getSelectedComboItems } from "@sale/js/sale_utils";
import { registry } from "@web/core/registry";
import { FloatField, floatField } from "@web/views/fields/float/float_field";
import { useService } from "@web/core/utils/hooks";


class QuotationRemarkField extends Component {
    static template = "sale.QuotationRemarkField";
    props = useProps({ ...standardFieldProps });
    editor = signal.ref();

    setup() {
        this.state = proxy({ editing: false, text: "", busy: false });
        this.notification = useService("notification");
        onPatched(() => {
            const editor = this.editor();
            if (editor && !editor.dataset.initialized) {
                editor.dataset.initialized = "1";
                editor.value = this.state.text;
                this.resize(editor);
                requestAnimationFrame(() => {
                    if (editor.isConnected && this.state.editing) {
                        editor.focus(); editor.select();
                    }
                });
            }
        });
    }
    get value() { return this.props.record.data[this.props.name] || ""; }
    edit() {
        if (this.props.readonly || this.state.busy) return;
        this.state.text = this.value;
        this.state.editing = true;
    }
    resize(editor) { editor.style.height = "auto"; editor.style.height = editor.scrollHeight + "px"; }
    input(event) { this.state.text = event.target.value; this.resize(event.target); }
    keydown(event) {
        if (event.key === "Escape") {
            event.preventDefault(); event.stopPropagation(); this.state.editing = false;
        } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault(); event.stopPropagation(); this.save();
        }
    }
    async save() {
        if (!this.state.editing || this.state.busy) return;
        this.state.busy = true;
        try {
            if (this.state.text !== this.value) await this.props.record.update({ [this.props.name]: this.state.text });
            this.state.editing = false;
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "备注保存失败，请重试。", { type: "danger" });
        } finally { this.state.busy = false; }
    }
}
registry.category("fields").add("sale_quotation_remark", { component: QuotationRemarkField, supportedTypes: ["text"] });

class CustomerQuotationDownload extends Component {
    static template = "sale.CustomerQuotationDownload";
    static components = { Dropdown, DropdownItem };
    props = useProps({ ...standardWidgetProps });
    setup() {
        this.action = useService("action"); this.orm = useService("orm");
        this.notification = useService("notification"); this.state = proxy({ busy: false });
    }
    async download(kind) {
        if (this.state.busy) return;
        this.state.busy = true;
        try {
            const order = this.props.record;
            if ((order.isNew || await order.isDirty()) && !await order.save()) return;
            if (kind === "pdf") {
                await this.action.doAction("sale.action_report_saleorder", { additionalContext: {
                    active_model: "sale.order", active_id: order.resId, active_ids: [order.resId],
                }});
            } else {
                const action = await this.orm.call("sale.order", "action_download_customer_xlsx", [[order.resId]]);
                await this.action.doAction(action);
            }
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "报价单下载失败，请重试。", { type: "danger" });
        } finally { this.state.busy = false; }
    }
}
registry.category("view_widgets").add("sale_customer_quotation_download", { component: CustomerQuotationDownload });

class SavedResultNumber extends FloatField {
    get known() { return this.props.record.data.saved_result_values?.[this.props.name] != null; }
    get formattedValue() { return this.known ? super.formattedValue : "—"; }
}
registry.category("fields").add("sale_saved_result_number", { ...floatField, component: SavedResultNumber });
class QuotationNumberField extends FloatField {
    static template = "sale.QuotationNumberField";
    setup() {
        super.setup(); this.action = useService("action"); this.orm = useService("orm");
        this.notification = useService("notification");
    }
    async openDetails() {
        const order = this.props.record.model.root;
        if ((order.isNew || await order.isDirty()) && !await order.save()) return;
        const line = this.props.record;
        if (!line.resId) return;
        try {
            const action = await this.orm.call("sale.order.line",
                this.props.name === "saved_cost_unit" ? "action_open_saved_cost_details" : "action_open_saved_quote_breakdown", [[line.resId]]);
            await this.action.doAction(action);
        } catch (error) {
            this.notification.add(error.data?.message || error.message, { type: "danger" });
        }
    }
}
class QuotationCostField extends QuotationNumberField {
    get formattedValue() {
        return this.props.record.data.saved_result_values?.[this.props.name] != null ? super.formattedValue : "—";
    }
}
registry.category("fields").add("sale_quotation_number", { ...floatField, component: QuotationNumberField, supportedTypes: ["float", "monetary"] });
registry.category("fields").add("sale_quotation_cost", { ...floatField, component: QuotationCostField, supportedTypes: ["float", "monetary"] });

class SavedResultMargin extends SavedResultNumber {
    static template = "sale.SavedResultMargin";
    get label() { return this.props.name === "saved_gross_margin" ? "毛利率：" : "净利率："; }
    get formattedValue() { return this.known ? new Intl.NumberFormat("zh-CN", {style:"percent",minimumFractionDigits:2,maximumFractionDigits:2}).format(this.value) : "—"; }
}
registry.category("fields").add("sale_saved_margin", { ...floatField, component: SavedResultMargin });

function getComboRecords(listRecords, record) {
    const comboRecords = [];

    if (record.data.product_type === 'combo') {
        // if currernt record is combo then we move forward util we find non combo line
        comboRecords.push(record);
        let index = listRecords.indexOf(record) + 1;

        while (index < listRecords.length) {
            const r = listRecords[index];
            if (
                !r.data.combo_item_id?.id
                || (
                    r.data.linked_line_id?.id !== record.resId
                    && r.data.linked_virtual_id !== record.data.virtual_id
                )
            ) {
                break;
            }
            comboRecords.push(r);
            index++;
        }

    } else if (record.data.combo_item_id?.id) {
        // if current record is combo item then we move backward util we find associated combo line
        // Here we assume that the record we get is the last item of the combo
        let index = listRecords.indexOf(record);
        while (index >= 0) {
            const r = listRecords[index];
            comboRecords.unshift(r);

            if (
                r.data.product_type === 'combo'
                && (
                    r.resId === record.data.linked_line_id?.id
                    || r.data.virtual_id === record.data.linked_virtual_id
                )
            ) {
                break;
            }
            index--;
        }
    }

    return comboRecords;
}

// Stage changes in the dialog; native order-line discounts remain the source of truth.
class OrderDiscountCoefficientsDialog extends Component {
    static template = "sale.OrderDiscountCoefficientsDialog";
    static components = { Dialog };
    props = useProps({ rows: t.array(t.object()), apply: t.function(), close: t.function() });

    setup() {
        this.state = proxy({
            rows: this.props.rows.map((row) => ({ ...row, value: String(row.coefficient) })),
            uniform: "", busy: false, error: "",
        });
        this.env.dialogData.dismiss = () => { if (!this.state.busy) this.props.close(); };
    }

    coefficient(value) {
        const number = Number(value);
        if (!String(value).trim() || !Number.isFinite(number) || number < 0) {
            throw new Error("请填写大于或等于 0 的折扣系数，例如 1 或 0.8。");
        }
        return number;
    }

    fillAll() {
        try {
            const value = String(this.coefficient(this.state.uniform));
            this.state.rows.forEach((row) => { row.value = value; });
            this.state.error = "";
        } catch (error) { this.state.error = error.message; }
    }

    onUniformKeydown(event) {
        if (event.key === "Enter") { event.preventDefault(); this.fillAll(); }
    }

    changeUniform(event) { this.state.uniform = event.target.value; this.state.error = ""; }

    change(row, event) { row.value = event.target.value; this.state.error = ""; }

    async apply() {
        if (this.state.busy) return;
        try {
            const values = this.state.rows.map((row) => ({
                id: row.id, coefficient: this.coefficient(row.value),
            }));
            this.state.error = "";
            this.state.busy = true;
            await this.props.apply(values);
            this.props.close();
        } catch (error) {
            this.state.error = error.data?.message || error.message || "修改失败，请重试。";
        } finally { this.state.busy = false; }
    }
}

export class SaleOrderLineListRenderer extends ProductLabelSectionAndNoteListRender {
    static template = "sale.SaleOrderLineListRenderer";
    static recordRowTemplate = 'sale.ListRenderer.RecordRow';

    setup() {
        super.setup();
        this.priceColumns.push('discount', 'quotation_discounted_unit', 'quotation_discounted_total');
        this.adjustingSectionQuantities = false;
        this.discountDialog = useService("dialog");

        // Native sizing writes widths to leaf headers. With a spanning header row
        // first, fixed table layout needs those same widths on individual columns.
        onMounted(() => {
            this.syncSavedResultColumnWidths();
            const header = this.tableRef()?.tHead;
            if (header) {
                this.savedResultWidthObserver = new MutationObserver(() => this.syncSavedResultColumnWidths());
                this.savedResultWidthObserver.observe(header, {
                    subtree: true, childList: true, attributes: true, attributeFilter: ["style"],
                });
            }
        });
        onMounted(() => {
            if (this.isInternalQuotationCost && this.tableRef()?.parentElement) {
                this.quotationInfoResizeObserver = new ResizeObserver(() => this.syncSavedResultColumnWidths());
                this.quotationInfoResizeObserver.observe(this.tableRef().parentElement);
            }
        });
        onPatched(() => this.syncSavedResultColumnWidths());
        onWillUnmount(() => {
            this.savedResultWidthObserver?.disconnect();
            this.quotationInfoResizeObserver?.disconnect();
        });

        useSubEnv({
            shouldCollapse: this.shouldCollapse.bind(this),
        });
    }

    syncSavedResultColumnWidths() {
        const table = this.tableRef();
        if (!table) return;
        const headers = table.querySelectorAll("thead th");
        const columns = table.querySelectorAll(":scope > colgroup.o_saved_result_columns > col");
        if (columns.length && headers.length === columns.length) {
            headers.forEach((header, index) => {
                if (columns[index].style.width !== header.style.width) {
                    columns[index].style.width = header.style.width;
                }
            });
        }
        this.syncFrozenQuotationInformation(table, headers);
    }

    get isInternalQuotationCost() {
        return (this.props.archInfo.className || "").split(/\s+/).includes("o_internal_quote_cost_list");
    }

    syncFrozenQuotationInformation(table, headers) {
        if (!this.isInternalQuotationCost || !headers.length) return;
        let count = this.hasSelectors ? 1 : 0;
        for (const column of this.columns) {
            const name = column.name || "";
            if (name.startsWith("saved_") || ["price_unit", "selection_discount_coefficient", "section_state"].includes(name)
                || column.type === "button_group") break;
            count++;
        }
        count = Math.min(count, headers.length - (this.hasActionsColumn ? 1 : 0));
        const offsets = [];
        let width = 0;
        for (let index = 0; index < count; index++) {
            offsets.push(width);
            width += headers[index].getBoundingClientRect().width;
        }
        const frozen = new Set();
        const pin = (cell, index, edge) => {
            frozen.add(cell);
            cell.classList.add("o_quote_frozen_info");
            cell.classList.toggle("o_quote_frozen_info_edge", edge);
            const value = `${offsets[index]}px`;
            if (cell.style.getPropertyValue("--quote-info-fixed-start") !== value) {
                cell.style.setProperty("--quote-info-fixed-start", value);
            }
        };
        for (let index = 0; index < count; index++) pin(headers[index], index, index === count - 1);
        const grouped = table.querySelector("thead .o_saved_result_group_header > .o_saved_group_base");
        if (grouped && count && grouped.colSpan === count) pin(grouped, 0, true);
        for (const row of table.querySelectorAll("tbody > tr.o_data_row")) {
            let index = 0;
            for (const cell of row.children) {
                const span = cell.colSpan || 1;
                // Section/note cells may span the financial columns; never freeze that whole span.
                if (index < count && index + span <= count) pin(cell, index, index + span === count);
                index += span;
            }
        }
        for (const cell of table.querySelectorAll(".o_quote_frozen_info")) {
            if (!frozen.has(cell)) {
                cell.classList.remove("o_quote_frozen_info", "o_quote_frozen_info_edge");
                cell.style.removeProperty("--quote-info-fixed-start");
            }
        }
    }

    savedResultButtonLabel(column) {
        const buttons = column.buttons || [];
        if (buttons.some(b => b.clickParams?.name === "action_open_saved_quote_breakdown")) return "面价明细";
        if (buttons.some(b => b.clickParams?.name === "action_open_saved_cost_details")) return "成本明细";
        return "";
    }
    get isCustomerQuotation() {
        return (this.props.archInfo.className || "").split(/\s+/).includes("o_customer_quotation_list");
    }

    createViewKey() {
        // The two workflow stages retain independent native optional-column preferences.
        const layout = this.isCustomerQuotation ? "customer-quotation-v4" : "internal-quote-cost-v1";
        return `${super.createViewKey()},${layout}`;
    }

    processAllColumn(columns, list) {
        const result = super.processAllColumn(columns, list);
        if (this.isCustomerQuotation) return result;
        return result.map(column => {
            if (column.buttons?.some(button => button.clickParams?.name === "action_open_saved_quote_breakdown")) {
                return { ...column, name: "saved_quote_breakdown", label: "面价明细", optional: "show" };
            }
            if (["saved_quote_amount_0", "saved_quote_amount_1"].includes(column.name)) {
                // One preference controls the native tax-exclusive and tax-inclusive variants.
                return { ...column, name: "saved_quote_amount", optional: "show" };
            }
            if (column.name === "price_unit" && column.type === "column_group") {
                return { ...column, optional: "show" };
            }
            return column;
        });
    }

    computeOptionalActiveFields() {
        if (!this.isCustomerQuotation) {
            const marker = `${this.keyOptionalFields},quote-results-optional-v1`;
            if (!browser.localStorage.getItem(marker)) {
                const stored = browser.localStorage.getItem(this.keyOptionalFields);
                if (stored !== null) {
                    const visible = new Set(stored.split(","));
                    for (const name of ["saved_quote_breakdown", "saved_fixed_quote", "saved_discounted_face",
                        "saved_quote_coefficient", "price_unit", "saved_quote_freight", "saved_quote_amount",
                        "saved_gross_margin", "saved_net_margin"]) visible.add(name);
                    browser.localStorage.setItem(this.keyOptionalFields, [...visible].join(","));
                }
                browser.localStorage.setItem(marker, "1");
            }
        }
        return super.computeOptionalActiveFields();
    }

    get optionalFieldGroups() {
        const groups = super.optionalFieldGroups;
        if (this.isCustomerQuotation || !this.allColumns.some(column =>
            column.name === "saved_quote_amount" && !this.evalColumnInvisible(column.column_invisible))) return groups;
        const total = { name: "saved_quote_amount", label: "报价总价", value: this.optionalActiveFields.saved_quote_amount };
        const first = groups[0] || { optionalFields: [] };
        const fields = [...first.optionalFields];
        const freight = fields.findIndex(field => field.name === "saved_quote_freight");
        fields.splice(freight < 0 ? fields.length : freight + 1, 0, total);
        return [{ ...first, optionalFields: fields }, ...groups.slice(1)];
    }

    get savedResultHeaderGroups() {
        if (this.isCustomerQuotation) return [];
        if (this.columns.filter(c => c.name?.startsWith("saved_")).length < 2) return [];
        const groups = [];
        const push = (kind, label) => {
            const last = groups.at(-1);
            if (last?.kind === kind) last.span++;
            else groups.push({ kind, label, span: 1, key: groups.length });
        };
        if (this.hasSelectors) push("base", "报价信息");
        for (const column of this.columns) {
            const name = column.name || "";
            const buttons = column.buttons || [];
            if (name.startsWith("saved_quote_") || ["saved_fixed_quote","saved_discounted_face","price_unit","selection_discount_coefficient"].includes(name)
                || buttons.some(b => b.clickParams?.name === "action_open_saved_quote_breakdown")) push("quote", "报价结果");
            else if (name.startsWith("saved_") && name !== "saved_result_state") push("cost", "成本数据");
            else if (column.type === "widget" && groups.at(-1)?.kind === "base") push("base", "报价信息");
            else if (name === "saved_result_state" || column.type === "widget" || buttons.some(b => b.clickParams?.name === "action_open_saved_cost_details")) push("actions", "原结果");
            else push("base", "报价信息");
        }
        if (this.hasOpenFormViewColumn) push("actions", "原结果");
        if (this.hasActionsColumn) push("actions", "原结果");
        return groups;
    }

    async openDiscountCoefficients() {
        if (this.props.readonly || this.openingDiscountCoefficients) return;
        this.openingDiscountCoefficients = true;
        const list = this.props.list;
        try {
            // Commit the active cell before reading values, without saving the order.
            if (!await list.leaveEditMode({ validate: true, canAbandon: false })) return;
            const { offset, limit } = list;
            let records;
            try {
                // Load through the native cache so unsaved and off-page lines are included.
                if (offset || list.records.length < list.count) {
                    await list.load({ offset: 0, limit: Math.max(list.count, 1) });
                }
                records = [...list.records];
            } finally {
                if (list.offset !== offset || list.limit !== limit) await list.load({ offset, limit });
            }
            const discountColumn = this.columns.find((column) => ["discount", "selection_discount_coefficient"].includes(column.name));
            const coefficientField = discountColumn?.name || "discount";
            records = records.filter((record) => record.data.product_id &&
                !record.data.display_type && !record.data.is_downpayment &&
                !this.isComboItem(record) && !this.isFieldReadonly(discountColumn, record));
            if (!records.length) {
                this.notificationService.add("当前订单没有可修改折扣系数的产品。", { type: "info" });
                return;
            }
            const order = list.model.root;
            const rows = records.map((record) => ({
                id: record.id,
                product: record.data.quotation_name || record.data.product_template_id?.display_name || record.data.product_id.display_name,
                quantity: record.data.product_uom_qty,
                coefficient: coefficientField === "selection_discount_coefficient" ? record.data.selection_discount_coefficient : Number((1 - (record.data.discount || 0) / 100).toFixed(8)),
            }));
            const close = this.discountDialog.add(OrderDiscountCoefficientsDialog, {
                rows,
                apply: async (values) => {
                    if (this.props.readonly) throw new Error("当前订单已不可修改。");
                    const byId = new Map(records.map((record) => [record.id, record]));
                    const changes = values.map(({ id, coefficient }) => {
                        const record = byId.get(id);
                        return { record, discount: coefficientField === "selection_discount_coefficient" ? coefficient : Number(((1 - coefficient) * 100).toFixed(8)) };
                    }).filter(({ record, discount }) => discount !== (record.data[coefficientField] ?? (coefficientField === "discount" ? 0 : 1)));
                    if (!changes.length) return;
                    const linesData = {};
                    for (const { record, discount } of changes) {
                        linesData[record.resId || record._virtualId] = {
                            ids: record.resId ? [record.resId] : [],
                            changes: { ...record._getChanges(), [coefficientField]: discount },
                            changed_fields: [coefficientField],
                        };
                    }
                    // Reuse the existing batch line-onchange to refresh line amounts and margins too.
                    const results = await list.model.orm.call("sale.order", "batch_onchange_sol", [
                        linesData,
                        { order_id: { ...order._getChanges(), ...(!order.isNew && { id: order.resId }) } },
                        getFieldsSpec(list.activeFields, list.fields, list.evalContext, { withInvisible: true }),
                    ], { context: list.context });
                    const original = changes.map(({ record }) => {
                        const snapshot = record._getChanges(record.data, { withReadonly: true });
                        const fields = [coefficientField, ...Object.keys(results[record.resId || record._virtualId] || {})];
                        return x2ManyCommands.update(record.resId || record._virtualId,
                            Object.fromEntries(fields.filter((name) => name in snapshot).map((name) => [name, snapshot[name]])));
                    });
                    const commands = changes.map(({ record, discount }) =>
                        x2ManyCommands.update(record.resId || record._virtualId, {
                            ...results[record.resId || record._virtualId], [coefficientField]: discount,
                        }));
                    try {
                        // One native update recalculates the order and keeps normal save/discard behavior.
                        await order.update({ order_line: commands });
                    } catch (error) {
                        await order.update({ order_line: original });
                        throw error;
                    }
                },
            });
            this.dialogClose.push(close);
        } catch (error) {
            this.notificationService.add(error.data?.message || error.message || "无法读取折扣系数，请重试。", { type: "danger" });
        } finally { this.openingDiscountCoefficients = false; }
    }

    get lineNumberOffset() {
        const { offset, records } = this.props.list;
        const savedRecords = records.filter((record) => !record.isNew);
        return Math.min(offset, ...savedRecords.map((record) => record.data.line_number_offset));
    }

    /**
     * Little hack to make sure we get correct title field everytime
     * while accessing comboColumns
     */
    get comboColumns() {
        return [
            this.productAndDescriptionColumn,
            ...this.props.aggregatedFields,
            "product_uom_qty",
            "discount",
        ];
    }

    get sectionColumns() {
        return [...super.sectionColumns, "line_number", "sol_qty", "sol_uom"];
    }

    getActiveColumns() {
        let activeColumns = super.getActiveColumns();
        // Hide the UOM column if the field is optional and not active
        const uomCol = activeColumns.find((col) => col.name === "sol_uom");
        if (uomCol) {
            const uomField = uomCol.fields.find((field) => field.name === "product_uom_id");
            if (!uomField || (uomField.optional && !this.optionalActiveFields[uomField.name])) {
                activeColumns = activeColumns.filter((col) => col.name !== "sol_uom");
            }
        }

        return activeColumns;
    }

    getRowClass(record) {
        let classNames = super.getRowClass(record);
        if (this.isCombo(record) || this.isComboItem(record)) {
            classNames = classNames.replace('o_row_draggable', '');
        }
        return `${classNames} ${this.isCombo(record) ? 'fw-bold' : ''}`;
    }

    /**
     * @override
     */
    focusToName(editRec) {
        if (editRec && editRec.isNew && this.isSection(editRec)) {
            // Don't always focus on `productAndDescriptionColumn` for sections since we are adding section_qty and
            // section_uom_id fields in section row.
            return;
        }
        super.focusToName(editRec);
    }

    /**
     * @override
     */
    getSectionAndNoteColumns(columns, record) {
        if (this.isNote(record)) {
            return super.getSectionAndNoteColumns(columns, record);
        }
        return this.getSectionColumns(columns);
    }

    getSectionColumns(columns) {
        const isSectionCol = (col) =>
            [this.productAndDescriptionColumn, ...this.sectionColumns].includes(col.name) || col.widget === "handle";

        let titleColspan = 1;
        let absorbingColumns = true;
        let titleCol;

        const sectionCols = [];

        for (const col of columns) {
            if (col.name === this.productAndDescriptionColumn) {
                titleCol = col;
                continue;
            }

            if (isSectionCol(col)) {
                if (titleCol) {
                    // Stop absorbing at the first section column after the title.
                    absorbingColumns = false;
                    sectionCols.push({ ...titleCol, colspan: titleColspan }, col);
                    // Empty titleCol so that we don't add it again if there are multiple section
                    // columns after this.
                    titleCol = null;
                } else {
                    sectionCols.push(col);
                }
                continue;
            }

            if (absorbingColumns) {
                // Absorb non-section columns into the title's colspan.
                titleColspan++;
                continue;
            }

            sectionCols.push({ ...col, invisible: "1", readonly: "1" });
        }

        return sectionCols;
    }

    isFieldReadonly(column, record) {
        return super.isFieldReadonly(column, record) || (
            this.isComboItem(record)
                && !['name', 'tax_ids', 'qty_delivered'].includes(column.name)
        );
    }

    isProductFieldActive() {
        return true;
    }

    getVisibleColumnGroupFields(column,record){
        const fields=super.getVisibleColumnGroupFields(column,record);
        if(column.name!==this.productAndDescriptionColumn)return fields;
        return [...fields.filter(field=>field.name!=="selection_values"),...fields.filter(field=>field.name==="selection_values")];
    }

    isColumnGroupFieldVisible(column, fieldInfo, record) {
        if (column.name === this.productAndDescriptionColumn) {
            if (this.isSectionOrNote(record)) return fieldInfo.name === "name";
            if (!this.isCustomerQuotation) {
                if (fieldInfo.name === "selection_values") return super.isColumnGroupFieldVisible(column, fieldInfo, record);
                return fieldInfo.name === "product_template_id";
            }
            if (fieldInfo.name === "quotation_name") return true;
            if (fieldInfo.name === "selection_values") return false;
            if (fieldInfo.name === "product_template_id") {
                return !record.data.product_id || !!this.optionalActiveFields.product_template_id;
            }
            if (fieldInfo.name === "product_id") return !!this.optionalActiveFields.product_id;
            return false;
        }
        return super.isColumnGroupFieldVisible(column, fieldInfo, record) && (
            !this.isCombo(record)
                || this.comboColumns.includes(column.name)
                || this.comboColumns.includes(fieldInfo.name)
        );
    }

    async onDeleteRecord(record) {
        if (this.isCombo(record)) {
            await clearSelectedComboItems(record);
        }
        await super.onDeleteRecord(record);
    }

    async moveCombo(record, direction) {
        const canProceed = await this.props.list.leaveEditMode({ canAbandon: false });
        if (!canProceed) return;

        const { movingRecords, targetRecords } = this.getComboSwapPairs(record, direction);
        return this.swapSections(movingRecords, targetRecords);
    }

    getComboSwapPairs(record, direction) {
        const comboRecords = getComboRecords(this.props.list.records, record);

        if (direction === 'up') {
            return {
                movingRecords: this.getPreviousRecords(record),
                targetRecords: comboRecords,
            };
        }
        if (direction === 'down') {
            return {
                movingRecords: comboRecords,
                targetRecords: this.getNextRecords(record),
            };
        }
        return { movingRecords: [], targetRecords: [] };
    }

    getPreviousRecords(record) {
        const { records } = this.props.list;
        const previousRecord = records[records.indexOf(record) - 1];

        if (previousRecord?.data.combo_item_id?.id){
            return getComboRecords(records, previousRecord);
        }
        return previousRecord ? [previousRecord] : false;
    }

    getNextRecords(record) {
        const { records } = this.props.list;
        const comboRecords = getComboRecords(records, record);

        const nextRecord = records[records.indexOf(record) + comboRecords.length];
        if (nextRecord?.data.product_type === 'combo'){
            return getComboRecords(records, nextRecord);
        }
        return nextRecord ? [nextRecord] : false;
    }

    canUseFormatter(column, record) {
        if (
            this.isCombo(record) &&
            this.props.aggregatedFields.includes(column.name)
        ) {
            return true;
        }
        return super.canUseFormatter(column, record);
    }

    // For totals on combo lines
    getFormattedValue(column, record) {
        if (this.isCombo(record) && this.props.aggregatedFields.includes(column.name)) {
            const total = getComboRecords(this.props.list.records, record)
                .reduce((total, record) => total + record.data[column.name], 0);

            const formatter = registry.category('formatters').get(column.fieldType, (val) => val);

            return formatter(total, {
                ...formatter.extractOptions?.(column),
                data: record.data,
                field: record.fields[column.name],
            });
        }
        return super.getFormattedValue(column, record);
    }

    isCombo(record) {
        return record.data.product_type === 'combo';
    }

    isComboItem(record) {
        return !!record.data.combo_item_id;
    }

    shouldDuplicateSectionItem(record) {
        return !this.isCombo(record) && !this.isComboItem(record);
    }

    displayDeleteIcon(record){
        return super.displayDeleteIcon(record) && !this.isComboItem(record);
    }
}

export class SaleOrderLineOne2Many extends ProductLabelSectionAndNoteOne2Many {
    static components = {
        ...ProductLabelSectionAndNoteOne2Many.components,
        ListRenderer: SaleOrderLineListRenderer,
    };

    setup() {
        super.setup();
        this.dialog = useService("dialog");
        this.orm = useService("orm");
    }

    async openRecord(record) {
        if (record.data.product_type === "combo") {
            const selectedComboItems = await getSelectedComboItems(this.orm, record, true);
            return openComboConfigurator({
                dialog: this.dialog,
                comboLineRecord: record,
                edit: true,
                selectedComboItems,
            });
        }
        if (!record.data.combo_item_id?.id) {
            return super.openRecord(record);
        }
    }
}
export const saleOrderLineOne2Many = {
    ...productLabelSectionAndNoteOne2Many,
    component: SaleOrderLineOne2Many,
    additionalClasses: sectionAndNoteFieldOne2Many.additionalClasses,
};

registry.category('fields').add('sol_o2m', saleOrderLineOne2Many);

// Keep the native percentage in storage while editing the multiplier used by sales.
export class SaleDiscountCoefficientField extends FloatField {
    get value() {
        return this.props.name === "selection_discount_coefficient" ? super.value : 1 - (super.value || 0) / 100;
    }

    parse(value) {
        if (!value.trim()) return 0;
        const coefficient = super.parse(value);
        if (!Number.isFinite(coefficient) || coefficient < 0) {
            throw new Error("折扣系数必须是大于或等于 0 的数字。");
        }
        return this.props.name === "selection_discount_coefficient" ? coefficient : Number(((1 - coefficient) * 100).toFixed(8));
    }
}

registry.category("fields").add("sale_discount_coefficient", {
    ...floatField,
    component: SaleDiscountCoefficientField,
});
