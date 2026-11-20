import { Component, onWillStart, proxy } from "@odoo/owl";
import { useBus, useService } from "@web/core/utils/hooks";
import { ListRenderer } from "@web/views/list/list_renderer";
import { listView } from "@web/views/list/list_view";
import { registry } from '@web/core/registry';
import { SaleListRenderer } from '../sale_onboarding_list/sale_onboarding_list_renderer';
import { saleFileUploadListView } from '../sale_file_upload_list/sale_file_upload_list_view';
import { SaleFileUploadListController } from '../sale_file_upload_list/sale_file_upload_list_controller';
import { Dashboard } from '../../js/dashboard/dashboard';

export class DashboardListRenderer extends SaleListRenderer {
    static template = 'sale.ListRenderer';
    static components = {
        ...SaleListRenderer.components,
        Dashboard,
    };
}

export class DashboardListController extends SaleFileUploadListController {
    get actionMenuProps() {
        const actionMenuProps = super.actionMenuProps;
        return {
            ...actionMenuProps,
            onActionExecuted: ({ noReload } = {}) => {
                actionMenuProps.onActionExecuted({ noReload });
                if (!noReload) {
                    this.env.bus.trigger("reload");
                }
            },
        };
    }
}

export const dashboardListView = Object.assign(Object.create(saleFileUploadListView), {
    Controller: DashboardListController,
    Renderer: DashboardListRenderer,
});

registry.category('views').add('sale_dashboard_list', dashboardListView);

const PRODUCT_CATALOG_FILTERS = [
    { key: "jingneng_cabinet", label: "机箱机柜", unit: "条主档", color: "orange" },
    { key: "jingneng_attachment", label: "附件", unit: "条目录", color: "purple" },
];

class JingnengProductCatalogFilters extends Component {
    static template = "sale.JingnengProductCatalogFilters";

    setup() {
        this.orm = useService("orm");
        this.state = proxy({ totals: {}, active: [] });
        const update = () => {
            this.state.active = this.searchItems.filter((item) => item.isActive).map((item) => item.name);
        };
        useBus(this.env.searchModel, "update", update);
        useBus(this.env.bus, "reload", () => this.loadCounts());
        onWillStart(() => {
            update();
            return this.loadCounts();
        });
    }

    get cards() {
        return PRODUCT_CATALOG_FILTERS;
    }

    get searchItems() {
        return this.env.searchModel.getSearchItems(
            (item) => PRODUCT_CATALOG_FILTERS.some((filter) => filter.key === item.name),
        );
    }

    async loadCounts() {
        this.state.totals = await this.orm.call("product.template", "get_jingneng_product_catalog_counts");
    }

    changeCategory(key) {
        const items = this.searchItems;
        const selected = items.find((item) => item.name === key);
        if (!selected) {
            return;
        }
        for (const item of items) {
            const active = item.name === key && !selected.isActive;
            if (item.isActive !== active) {
                this.env.searchModel.toggleSearchItem(item.id);
            }
        }
    }
}

class JingnengProductCatalogListRenderer extends ListRenderer {
    static template = "sale.JingnengProductCatalogListRenderer";
    static components = { ...ListRenderer.components, JingnengProductCatalogFilters };

    get catalogKind() {
        const filters = this.env.searchModel?.getSearchItems((item) => item.isActive) || [];
        const cabinet = filters.some((item) => item.name === "jingneng_cabinet");
        const attachment = filters.some((item) => item.name === "jingneng_attachment");
        if (cabinet !== attachment) {
            return cabinet ? "cabinet" : "attachment";
        }
        const categories = new Set((this.props.list.records || []).map(
            (record) => record.data.categ_id?.display_name,
        ));
        if (categories.size === 1 && categories.has("机箱机柜")) return "cabinet";
        if (categories.size === 1 && categories.has("附件")) return "attachment";
        return "all";
    }

    processAllColumn(columns, list) {
        const kind = this.catalogKind;
        return super.processAllColumn(columns, list)
            .filter((column) => kind !== "cabinet" || column.name !== "jingneng_catalog_series")
            .map((column) => {
                if (column.name === "name") {
                    return { ...column,
                        label: kind === "cabinet" ? "产品系列" : kind === "attachment" ? "附件名称" : "名称",
                        attrs: { ...column.attrs, width: kind === "cabinet" ? "[160,260]" : "[320,600]" },
                    };
                }
                if (column.name === "jingneng_catalog_specification") {
                    return { ...column, label: kind === "cabinet" ? "明细" : "规格",
                        attrs: { ...column.attrs, width: kind === "cabinet" ? "[600]" : "[240]" },
                    };
                }
                if (column.name === "categ_id" || column.name === "jingneng_catalog_series") {
                    return { ...column, attrs: { ...column.attrs,
                        width: column.name === "categ_id" ? "[120,180]" : "[240,480]",
                    } };
                }
                return column;
            });
    }

    getCellClass(column, record) {
        const classes = super.getCellClass(column, record);
        return column.name === "jingneng_catalog_specification" && this.catalogKind === "cabinet"
            ? `${classes} o_list_text`
            : classes;
    }
}

registry.category("views").add("sale_jingneng_product_catalog", {
    ...listView,
    Renderer: JingnengProductCatalogListRenderer,
});

