import { Component, onWillUnmount, proxy, t, useProps } from "@odoo/owl";
import { useBus, useService } from "@web/core/utils/hooks";
import { Domain } from "@web/core/domain";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { useDropdownState } from "@web/core/dropdown/dropdown_hooks";
import { _t } from '@web/core/l10n/translation';
import { FileUploadListRenderer } from '@account/views/file_upload_list/file_upload_list_renderer';

class JingnengCustomerFilter extends Component {
    static template="sale.JingnengCustomerFilter";
    static components={Dropdown};
    props=useProps({fieldName:t.string().optional("partner_id"),label:t.string().optional("客户"),unit:t.string().optional("家")});
    setup() {
        this.orm=useService("orm");
        this.dropdown=useDropdownState();
        this.state=proxy({query:"",items:[],ids:[],loading:false,error:"",more:false,active:false});
        this.names={};this.revision=0;this.disposed=false;
        const sync=()=>{this.state.active=this.filters.length>0;};
        useBus(this.env.searchModel,"update",sync);sync();
        onWillUnmount(()=>{this.disposed=true;this.revision++;clearTimeout(this.searchTimer);});
    }
    get filters() {
        const search=this.env.searchModel;
        return search.query.map(q=>search.searchItems[q.searchItemId]).filter(i=>
            i?.jingnengColumnFilter===this.props.fieldName ||
            (this.props.fieldName==="partner_id" && i?.jingnengCustomerFilter));
    }
    get active() { return this.state.active; }
    async beforeOpen() {
        clearTimeout(this.searchTimer);
        this.state.query="";this.state.ids=[];
        for(const item of this.filters) {
            this.state.ids.push(...(item.columnFilterIds || item.customerIds || []));
            Object.assign(this.names,item.columnFilterNames || item.customerNames || {});
        }
        this.state.ids=[...new Set(this.state.ids)];
        await this.load();
    }
    search(event) {
        this.state.query=event.target.value;
        clearTimeout(this.searchTimer);
        this.searchTimer=setTimeout(()=>this.load(),250);
    }
    async load() {
        const revision=++this.revision;
        this.state.loading=true;this.state.error="";
        const search=this.env.searchModel;
        const domain=Domain.removeDomainLeaves(search.domain,[this.props.fieldName]).toList(search.domainEvalContext);
        const query=this.state.query.trim();
        if(query)domain.push([this.props.fieldName+".name","ilike",query]);
        try {
            const result=await this.orm.webReadGroup("sale.order",domain,[this.props.fieldName],["__count"],{
                limit:80,order:this.props.fieldName+" asc",context:search.context,
            });
            if(this.disposed||revision!==this.revision)return;
            this.state.items=result.groups
                .filter(g=>g[this.props.fieldName] || this.props.fieldName==="user_id")
                .map(g=>{
                    const value=g[this.props.fieldName];
                    return value ? {id:value.id ?? value[0],name:value.display_name ?? value[1]} :
                        {id:false,name:"未分配销售人员"};
                });
            for(const item of this.state.items)this.names[item.id]=item.name;
            this.state.more=result.length>this.state.items.length;
        } catch {
            if(!this.disposed&&revision===this.revision)this.state.error=this.props.label+"加载失败，请重试。";
        } finally {
            if(!this.disposed&&revision===this.revision)this.state.loading=false;
        }
    }
    toggle(id,checked) {
        this.state.ids=checked ? [...new Set([...this.state.ids,id])] : this.state.ids.filter(value=>value!==id);
    }
    clear() {
        for(const filter of this.filters)this.env.searchModel.deactivateGroup(filter.groupId);
        this.state.ids=[];this.dropdown.close();
    }
    apply() {
        if(!this.state.ids.length||this.state.loading||this.state.error)return;
        const ids=[...this.state.ids],names=Object.fromEntries(ids.map(id=>[id,this.names[id]]));
        for(const filter of this.filters)this.env.searchModel.deactivateGroup(filter.groupId);
        this.env.searchModel.createNewFilters([{
            description:this.props.label+"："+ids.map(id=>names[id]).join("、"),
            domain:[[this.props.fieldName,"in",ids]],invisible:"True",
            jingnengColumnFilter:this.props.fieldName,columnFilterIds:ids,columnFilterNames:names,
        }]);
        this.dropdown.close();
    }
}

export class SaleFileUploadListRenderer extends FileUploadListRenderer {
    static template="sale.CustomerFilterListRenderer";
    static components={...FileUploadListRenderer.components,JingnengCustomerFilter};
    setup() {
        super.setup();
        this.dropZoneTitle = _t("Import a request for quotation from a customer");
        this.dropZoneDescription = _t(`
            If your customer runs on Odoo 18 or higher, customer data and sales order lines
            will be automatically created. Any other pdf containing an attached
            UBL-RequestForQuotation file will work as well.
        `);
    }
}
