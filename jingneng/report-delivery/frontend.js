// Appended to the existing sale dashboard asset; no new application asset.
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";
import { useChart } from "@web/core/utils/chart_hook";
import { getColor } from "@web/core/colors/colors";
import { useSetupAction } from "@web/search/action_hook";
import { cookie } from "@web/core/browser/cookie";
import { downloadFile } from "@web/core/network/download";

class JingnengReportChart extends Component {
    static template = "sale.JingnengReportChart";
    props = useProps({ config: Object, label: String });
    setup() { this.chart = useChart(() => this.props.config); }
}

const JN_RESULTS = { won: "中标", lost: "失标", pending: "待反馈" };
const JN_DIMENSIONS = { sales: "销售", people: "销售人员", products: "产品", customers: "客户" };
const JN_SCOPES = {sale:"销售订单", contract:"合同", quote:"报价单／已发送", valid:"全部有效单据"};
const jnSum = (items, fn) => items.reduce((sum, item) => sum + fn(item), 0);
const jnRate = (rows) => {
    const won = rows.filter(r => r.result === "won").length;
    const closed = rows.filter(r => r.result !== "pending").length;
    return closed ? `${(100 * won / closed).toFixed(1)}%` : "—";
};

// Pure aggregation shared by graphs, pivot cells, exports and drill-downs.
export function buildJingnengReport(rows, filters, dimension) {
    const query = filters.search.trim().toLocaleLowerCase();
    const cohort = rows.map(row => {
        const lines = row.lines.filter(l => (!filters.kind || l.kind === filters.kind) &&
            (!filters.unit || String(l.unit_id) === filters.unit));
        const lineAmount = jnSum(lines, l => l.amount);
        return {...row, lines, amount: dimension === "products" && filters.analysis === "sales" ? lineAmount : row.amount};
    }).filter(r => (!filters.kind && !filters.unit || r.lines.length) &&
        (!query || [r.name,r.customer,r.person,r.notes,...r.lines.map(l => l.name)].join(" ").toLocaleLowerCase().includes(query)));
    const groupFor = (row) => {
        if (dimension === "products") return [...new Map(row.lines.map(l => [String(l.product),l.name])).entries()];
        if (dimension === "people") return [[String(row.person_id),row.person]];
        if (dimension === "customers") return [[String(row.customer_id),row.customer]];
        return [[row.date.slice(0,7),row.date.slice(0,7)]];
    };
    const filtered = cohort.filter(r => (filters.result === "all" || filters.analysis !== "bids" || r.result === filters.result) &&
        (!filters.month || r.date.startsWith(filters.month)) &&
        (!filters.reason || r.result === "lost" && r.reason === filters.reason) &&
        (!filters.group || groupFor(r).some(([id]) => id === filters.group)));
    const metric = (items, key) => {
        if (filters.metric === "qty") return jnSum(items, r => jnSum(r.lines.filter(l => dimension !== "products" || String(l.product) === key), l => l.qty));
        if (filters.metric === "amount") {
            if (dimension === "products" && filters.analysis === "sales")
                return jnSum(items,r => jnSum(r.lines.filter(l => String(l.product) === key),l => l.amount));
            return jnSum(items,r => r.amount);
        }
        return items.length;
    };
    const groups = new Map();
    for (const row of filtered) for (const [id,name] of groupFor(row)) {
        if (!groups.has(id)) groups.set(id,{id,name,rows:[]});
        groups.get(id).rows.push(row);
    }
    const ranking = [...groups.values()].map(g => ({...g, count:g.rows.length,
        amount: dimension === "products" && filters.analysis === "sales" ? jnSum(g.rows,r=>jnSum(r.lines.filter(l=>String(l.product)===g.id),l=>l.amount)) : jnSum(g.rows,r=>r.amount),
        qty: dimension === "products" ? jnSum(g.rows,r=>jnSum(r.lines.filter(l=>String(l.product)===g.id),l=>l.qty)) : 0,
        value:metric(g.rows,g.id), rate:jnRate(g.rows),
        won:g.rows.filter(r=>r.result==="won").length, lost:g.rows.filter(r=>r.result==="lost").length,
        pending:g.rows.filter(r=>r.result==="pending").length,
    })).sort((a,b)=> dimension === "sales" ? a.id.localeCompare(b.id) : b.value-a.value || a.name.localeCompare(b.name,"zh"));
    const months = [];
    if(filters.start && filters.end) {
        let month=filters.start.slice(0,7),last=filters.end.slice(0,7);
        while(month<=last && months.length<122) { months.push(month);const [year,number]=month.split('-').map(Number);month=number===12?`${year+1}-01`:`${year}-${String(number+1).padStart(2,'0')}`; }
    } else months.push(...[...new Set(cohort.map(r=>r.date.slice(0,7)))].sort());
    const trend = months.map(month=>{
        const items=filtered.filter(r=>r.date.startsWith(month));
        return {month, rows:items, total:filters.metric==="amount" ? jnSum(items,r=>r.amount) : filters.metric==="qty" ? jnSum(items,r=>jnSum(r.lines,l=>l.qty)) : items.length,
            ...Object.fromEntries(Object.keys(JN_RESULTS).map(result=>[result,filters.metric==="amount" ? jnSum(items.filter(r=>r.result===result),r=>r.amount) : items.filter(r=>r.result===result).length]))};
    });
    return {cohort,filtered,ranking,months,trend,metric,groupFor};
}

export class JingnengReports extends Component {
    static template = "sale.JingnengReports";
    static components = { JingnengReportChart };
    props = useProps({...standardActionServiceProps});
    setup() {
        this.orm=useService("orm"); this.action=useService("action"); this.notification=useService("notification");
        this.state=proxy({loading:true,error:"",data:null,view:"graph",section:"summary",details:false,page:0,sort:"value",
            filters:{analysis:"bids",period:"6",start:"",end:"",currency:"",scope:"sale",search:"",kind:"",unit:"",result:"all",metric:"count",group:"",month:"",reason:""}});
        const saved=this.props.state?.jingnengReport || this.props.globalState?.jingnengReport;
        if(saved) { Object.assign(this.state,{view:saved.view,section:saved.section,details:saved.details,page:saved.page,sort:saved.sort});Object.assign(this.state.filters,saved.filters); }
        const exportState=()=>({jingnengReport:{filters:{...this.state.filters},view:this.state.view,section:this.state.section,details:this.state.details,page:this.state.page,sort:this.state.sort}});
        useSetupAction({getLocalState:exportState,getGlobalState:exportState});
        this.request=0;
        onWillStart(()=>this.load());
    }
    get dimension() { return this.props.action.params?.dimension || "sales"; }
    get bids() { return this.state.filters.analysis === "bids"; }
    get product() { return this.dimension === "products"; }
    get title() { return `${JN_DIMENSIONS[this.dimension]}${this.bids ? "报价结果分析" : "业绩分析"}`; }
    get amountLabel() { return this.bids ? "含税报价额" : this.product ? "含税产品金额" : `含税${JN_SCOPES[this.state.filters.scope]}金额`; }
    get metricLabel() { return this.state.filters.metric === "amount" ? this.amountLabel : this.state.filters.metric === "qty" ? `产品数量（${this.unitName}）` : this.bids ? "报价单数" : "单据数量"; }
    get groupLabel() { return {sales:"月份",people:"销售人员",products:"产品",customers:"客户"}[this.dimension]; }
    get currency() { return this.state.data?.currencies.find(c=>c.id===this.state.data.currency); }
    get unitName() { return this.units.find(u=>String(u.id)===this.state.filters.unit)?.name || "请选择单位"; }
    get units() { return [...new Map((this.state.data?.rows || []).flatMap(r=>r.lines).filter(l=>!this.state.filters.kind || l.kind===this.state.filters.kind).map(l=>[l.unit_id,{id:l.unit_id,name:l.unit}])).values()]; }
    get vm() { return buildJingnengReport(this.state.data?.rows||[],this.state.filters,this.dimension); }
    number(value) { return new Intl.NumberFormat("zh-CN",{maximumFractionDigits:2}).format(value || 0); }
    money(value) { return `${this.currency?.symbol || ""} ${new Intl.NumberFormat("zh-CN",{minimumFractionDigits:2,maximumFractionDigits:2}).format(value || 0)}`; }
    value(value) { return this.state.filters.metric==="amount" ? this.money(value) : this.number(value); }
    resultLabel(row) { return JN_RESULTS[row.result]; }
    reasonName(key) { return this.state.data?.reasons[key] || "未登记"; }
    get cards() {
        const rows=this.vm.cohort;
        if(this.bids) {
            const cards=[{key:"all",label:"有效报价",value:this.number(rows.length),sub:this.money(jnSum(rows,r=>r.amount))},
                ...Object.entries(JN_RESULTS).map(([key,label])=>{const items=rows.filter(r=>r.result===key);return {key,label,value:this.number(items.length),sub:this.money(jnSum(items,r=>r.amount))};})];
            const won=rows.filter(r=>r.result==="won").length, lost=rows.filter(r=>r.result==="lost").length;
            cards.push({key:"rate",label:"中标率",value:jnRate(rows),sub:`${won} 中标 / ${won+lost} 已反馈`});
            return cards;
        }
        const amount=jnSum(rows,r=>r.amount);
        return [{key:"all",label:this.amountLabel,value:this.money(amount),sub:"按当前范围汇总"},
            {key:"orders",label:`${JN_SCOPES[this.state.filters.scope]}数量`,value:this.number(rows.length),sub:"按单据去重"},
            {key:"customers",label:"客户数",value:this.number(new Set(rows.map(r=>r.customer_id)).size),sub:"按客户去重"},
            {key:"average",label:"平均单据金额",value:this.money(rows.length ? amount/rows.length : 0),sub:"金额 / 单据数量"}];
    }
    get reasons() {
        const groups=new Map();
        for(const row of this.vm.cohort.filter(r=>r.result==="lost")) {
            if(!groups.has(row.reason))groups.set(row.reason,{id:row.reason,name:this.reasonName(row.reason),rows:[]});
            groups.get(row.reason).rows.push(row);
        }
        return [...groups.values()].map(g=>({...g,count:g.rows.length,amount:jnSum(g.rows,r=>r.amount)})).sort((a,b)=>b.count-a.count);
    }
    get ranking() {
        const data=this.vm.ranking;
        return this.state.sort==="rate" && this.bids ? data.toSorted((a,b)=>{
            const n=g=>g.won+g.lost ? g.won/(g.won+g.lost) : -1; return n(b)-n(a);
        }) : data;
    }
    rankWidth(group) {
        const measure=g=>this.state.sort==='rate' && this.bids ? (g.won+g.lost ? g.won/(g.won+g.lost) : 0) : Math.abs(g.value);
        return `${Math.max(0,Math.min(100,measure(group)/Math.max(.000001,...this.ranking.map(measure))*100))}%`;
    }
    get groupPages() { return Math.max(1,Math.ceil(this.ranking.length/20)); }
    get detailRows() { return this.vm.filtered.toSorted((a,b)=>b.date.localeCompare(a.date)||b.id-a.id); }
    get pageCount() { return Math.max(1,Math.ceil((this.state.details?this.detailRows.length:this.ranking.length)/20)); }
    get pagedRows() { return (this.state.details?this.detailRows:this.ranking).slice(this.state.page*20,this.state.page*20+20); }
    get chips() {
        const f=this.state.filters;
        return [f.group && {key:"group",label:`${this.groupLabel}：${this.vm.ranking.find(g=>g.id===f.group)?.name||f.group}`},
            f.month&&{key:"month",label:`月份：${f.month}`}, f.reason&&{key:"reason",label:`失标原因：${this.reasonName(f.reason)}`}].filter(Boolean);
    }
    async load() {
        const sequence=++this.request; this.state.loading=true; this.state.error="";
        try {
            const data=await this.orm.call("sale.report","get_jingneng_report_data",[this.state.filters]);
            if(sequence!==this.request)return;
            this.state.data=data; this.state.filters.currency=String(data.currency);
            this.state.filters.start=data.start; this.state.filters.end=data.end;
        } catch(error) { if(sequence===this.request)this.state.error=error.data?.message || error.message || "报表加载失败，请重试。"; }
        finally { if(sequence===this.request)this.state.loading=false; }
    }
    async filter(key,event) {
        const value=event.target.value;
        this.state.filters[key]=value; this.state.page=0;
        if(["period","start","end","currency","scope"].includes(key)) {
            this.clearDrill();
            if(this.state.filters.period!=="custom" || this.state.filters.start && this.state.filters.end) await this.load();
        }
        if(key==="kind"){this.state.filters.unit="";if(this.state.filters.metric==="qty")this.state.filters.metric="count";}
        if(key==="unit" && !value && this.state.filters.metric==="qty")this.state.filters.metric="count";
        if(key==="result" && value!=="lost")this.state.section="summary";
    }
    async analysis(value) {
        this.state.filters.analysis=value; this.state.filters.result="all";this.state.filters.metric="count";
        this.state.section="summary";this.clearDrill();await this.load();
    }
    clearDrill() { Object.assign(this.state.filters,{group:"",month:"",reason:""});this.state.details=false;this.state.page=0; }
    removeChip(key) { this.state.filters[key]="";this.state.page=0; }
    drill(key,value,result) { this.state.filters[key]=value; if(result)this.state.filters.result=result;this.state.details=true;this.state.page=0; }
    card(key) { if(!this.bids)return;this.state.section="summary";this.state.filters.result=key==="rate"?"all":key;this.state.details=true;this.state.page=0; }
    changeView(view) { this.state.view=view;this.state.page=0; }
    page(offset) { this.state.page=Math.max(0,Math.min(this.pageCount-1,this.state.page+offset)); }
    async openOrder(id) { await this.action.doAction({type:"ir.actions.act_window",res_model:"sale.order",res_id:id,views:[[false,"form"]],target:"current"}); }
    async feedback(id) { const action=await this.orm.call("sale.order","action_open_customer_feedback",[[id]]);await this.action.doAction(action,{onClose:()=>this.load()}); }
    async missing() { await this.action.doAction({name:"报价单",type:"ir.actions.act_window",res_model:"sale.order",views:[[false,"list"],[false,"form"]],domain:[["quotation_first_sent_at","=",false],["state","in",["draft","sent"]]]}); }
    pivotValue(group,month) { return this.value(this.vm.metric(group.rows.filter(r=>r.date.startsWith(month)),group.id)); }
    get chartConfig() {
        const vm=this.vm, bids=this.bids;
        const keys=bids?Object.keys(JN_RESULTS):["total"];
        return {type:"bar",data:{labels:vm.trend.map(r=>r.month),datasets:keys.map((key,i)=>({
            label:bids?JN_RESULTS[key]:this.metricLabel,data:vm.trend.map(r=>r[key]),backgroundColor:getColor(bids?[10,13,19][i]:0,cookie.get('color_scheme'),'lg'),borderRadius:2,maxBarThickness:46,
        }))},options:{responsive:true,maintainAspectRatio:false,animation:false,
            plugins:{legend:{display:bids,position:"bottom"},tooltip:{callbacks:{label:c=>`${c.dataset.label}：${this.value(c.raw)}`}}},
            scales:{x:{stacked:bids,grid:{display:false}},y:{stacked:bids,beginAtZero:true,ticks:{precision:this.state.filters.metric==="count"?0:undefined}}},
            onClick:(_event,elements)=>{if(!elements.length)return;const item=elements[0];this.drill("month",vm.trend[item.index].month,bids?keys[item.datasetIndex]:undefined);}}};
    }
    get reasonChartConfig() {
        const reasons=this.reasons;
        return {type:"bar",data:{labels:reasons.map(r=>r.name),datasets:[{label:"失标报价单数",data:reasons.map(r=>r.count),backgroundColor:getColor(13,cookie.get('color_scheme'),'lg'),maxBarThickness:34}]},
            options:{indexAxis:"y",responsive:true,maintainAspectRatio:false,animation:false,plugins:{legend:{display:false}},scales:{x:{beginAtZero:true,ticks:{precision:0}},y:{grid:{display:false}}},
                onClick:(_event,elements)=>{if(elements.length)this.drill("reason",reasons[elements[0].index].id,"lost");}}};
    }
    export() {
        const rows=this.detailRows;
        const header=["单据编号","客户","销售人员",this.bids?"首次正式发送日期":"统计日期",this.bids&&this.product?"关联整单含税报价额（不可跨产品相加）":this.amountLabel,"币种",...(this.product?["产品","分类","数量","单位","明细含税金额"]:[]),...(this.bids?["客户反馈结果","反馈日期","失标原因","下次跟进日期","反馈备注"]:[])];
        const values=rows.flatMap(r=>(this.product?r.lines:[null]).map(line=>[r.name,r.customer,r.person,r.date,r.amount,this.currency?.name,...(line?[line.name,line.kind,line.qty,line.unit,line.amount]:[]),...(this.bids?[JN_RESULTS[r.result],r.feedback,r.result==="lost"?this.reasonName(r.reason):"",r.followup,r.notes]:[])]));
        const cell=v=>'"'+String(v??"").replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"';
        const contents="\uFEFF"+[header,...values].map(r=>r.map(cell).join(",")).join("\r\n");
        downloadFile(contents,`京能-${this.title}-${this.state.data.start}-${this.state.data.end}.csv`,"text/csv;charset=utf-8");
    }
}
registry.category("actions").add("sale.jingneng_reports",JingnengReports);
