import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
const source=await fs.readFile(new URL('./frontend.js',import.meta.url),'utf8');
const start=source.indexOf('const JN_RESULTS');const end=source.indexOf('export class JingnengReports');
const {buildJingnengReport:build}=await import('data:text/javascript;base64,'+Buffer.from(source.slice(start,end)).toString('base64'));
const base={search:'',kind:'',unit:'',analysis:'sales',result:'all',month:'',reason:'',group:'',metric:'count',start:'2026-06-01',end:'2026-11-19'};
const line=(product,unit_id,qty,amount)=>({product,name:'产品'+product,kind:product===1?'机箱机柜':'附件',unit_id,unit:unit_id===1?'台':'个',qty,amount});
const rows=[{id:1,name:'单据1',date:'2026-11-03',person:'人员1',person_id:1,customer:'客户一',customer_id:1,result:'won',amount:300,lines:[line(1,1,2,100),line(2,2,5,200)]},
 {id:2,name:'单据2',date:'2026-11-04',person:'人员1',person_id:1,customer:'客户二',customer_id:2,result:'lost',amount:-40,lines:[line(1,1,-1,-40)]}];
let vm=build(rows,base,'products');assert.equal(vm.cohort.length,2);assert.equal(vm.ranking.find(r=>r.id==='1').count,2);assert.equal(vm.ranking.find(r=>r.id==='1').amount,60);assert.equal(vm.ranking.find(r=>r.id==='2').amount,200);
assert.equal(vm.months.length,6);assert.equal(vm.trend[0].total,0);
vm=build(rows,{...base,metric:'qty',unit:'1'},'products');assert.equal(vm.ranking.length,1);assert.equal(vm.ranking[0].value,1);
vm=build(rows,{...base,metric:'amount'},'products');assert.equal(vm.trend.at(-1).total,260);assert.equal(vm.ranking[0].amount+vm.ranking[1].amount,260);
vm=build(rows,{...base,analysis:'bids',result:'won'},'customers');assert.equal(vm.cohort.length,2);assert.equal(vm.filtered.length,1);assert.equal(vm.ranking[0].amount,300);
vm=build(rows,{...base,analysis:'bids',month:'2026-11',group:'2'},'customers');assert.equal(vm.filtered[0].id,2);
console.log('PASS mixed product lines, distinct order counts, signed line amounts, unit isolation, zero months, stable KPI cohort and drill filters');
