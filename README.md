# 京能 Odoo 定制版

保存京能当前原生安装版的完整 Odoo 社区源码及全部定制修改，供备份、审阅和后续开发使用。

归档日期：**2026-11-20（北京时间）**。运行环境仍是原安装版 `127.0.0.1:8069`，数据库 `odoo_customer_demo`。建立本仓库不会切换、重装或重启现有 Odoo。

## 源码与修改范围

- 完整源码包含 **34,874 个分发源码与静态资源文件**，保留原安装包所含简体中文资源。
- 对当前运行版与原安装包逐文件比较，得到 **58 个修改文件**，涵盖各对话的已落地修改。
- 基线提交保存原安装包；其后的定制与配置补充提交保存当前修改、配置参考和交付资料。
- 基线为 Odoo Community `20.0-20261112` Windows 分发，标注的上游提交为 `53882d6696307bd63504f79ae8aa421b7468a333`。不额外声称包含未随包提供的其他语种翻译及完整上游历史。
- [逐文件修改清单](jingneng/audit/changed-source.json)；[运行版文件哈希清单](jingneng/audit/current-source-manifest.json)；[原版说明](UPSTREAM_README.md)。

## 已实现的主要修改

1. **报价流程**：草稿、报价单、已发送报价单、合同、销售订单；中标、失标及待反馈独立登记，取消不自动视为失标。
2. **列表与分析**：订单编号、人员与客户筛选、列设置、阶段日期、按单据汇总的进度时间轴、销售及报价结果分析。
3. **图纸与选型**：页签前置、多文件上传、后台识图与旋正、滚轮缩放与中键拖动、手动框选尺寸直接回填、有图与无图录入。
4. **产品与附件**：机箱机柜/附件分类、11个柜型系列、材质/门型/料厚/喷塑参数；附件汉字重叠率阈值 **60%**，人工选择保留。
5. **单柜与并柜**：宽度表达式识别柜位、每柜门型与逐柜/整组附件；单柜自动归属；附件实测尺寸不擅自从柜体尺寸推定。
6. **真实计价**：复用原报价服务规格匹配及计算，当前快照与历史结果分开保存；材料按材质取价，原软件分项折扣；参数变化后要求重算并阻止旧金额输出。
7. **报价与成本界面**：面价、固定报价、折扣面价、系数、报价、运费及总价，内部成本分组、附加利润、人工名称/备注保护、规格列及报价信息组冻结。
8. **客户输出**：京能空白导入模板和对客八列PDF/表格；内部成本不进入客户白名单；销售订单及形式发票保留各自原逻辑。
9. **物流解耦**：当前数据库暂停销售自动库存补货。确认合同、订单加行及改数量不自动出库/补货，不假记交付；合同和已交付数量下限保护保留。

详见 [CHANGELOG.md](jingneng/docs/CHANGELOG.md)。

## 目录

| 目录/文件 | 内容 |
|---|---|
| `odoo/`、`addons/`、`odoo-bin` | 完整运行版社区源码与资源，保持原分发布局 |
| `jingneng/native/` | 当前Windows管理/初始化脚本及组件来源 |
| `jingneng/integration/` | 既有原报价服务读取、正式案例导入及客户清理脚本，业务输入不上传 |
| `jingneng/report-delivery/` | 既有报表实施和验收脚本 |
| `jingneng/config/` | 非敏感参数、界面/动作/字段参考快照、模块清单及无密钥示例 |
| `jingneng/design/` | 交互设计稿；商务工作台目前仅方案，未部署 |
| `jingneng/audit/` | 全部源码修改、哈希及归档核验记录 |

## 部署与依赖

先阅读 [部署与配置说明](jingneng/docs/DEPLOYMENT.md)。这是源码归档，不包含Python/PostgreSQL/wkhtmltopdf二进制运行时或客户数据库。

计价依赖原报价服务及客户端规则源码，识图依赖原识图运行库和转换组件；路径、授权与组件须在目标环境正确配置。本仓库不提供新的计价服务，也不使用模拟价格替代缺失规则。

`*.snapshot.json` 是配置审阅参考，不是数据库备份或任意数据库可无条件执行的迁移。无法解析为XML ID的关系保留本机ID标记，迁移时需重新对应。

## 不进入仓库的内容

本机密码、API密钥、会话、数据库转储、客户业务记录、真实成本快照、上传图纸、日志、缓存和二进制运行库不上传。读取这些资源的代码完整保留；连接配置只提供无密钥示例。

## 许可

保留 [LICENSE](LICENSE)、[COPYRIGHT](COPYRIGHT) 及各模块自身许可。原报价软件和识图依赖的许可不因本仓库而改变。

---

## 上游项目说明

# Odoo

[![Build Status](https://runbot.odoo.com/runbot/badge/flat/1/master.svg)](https://runbot.odoo.com/runbot)
[![Tech Doc](https://img.shields.io/badge/master-docs-875A7B.svg?style=flat&colorA=8F8F8F)](https://www.odoo.com/documentation/master)
[![Help](https://img.shields.io/badge/master-help-875A7B.svg?style=flat&colorA=8F8F8F)](https://www.odoo.com/forum/help-1)
[![Nightly Builds](https://img.shields.io/badge/master-nightly-875A7B.svg?style=flat&colorA=8F8F8F)](https://nightly.odoo.com/)

Odoo is a suite of web based open source business apps.

The main Odoo Apps include an [Open Source CRM](https://www.odoo.com/page/crm),
[Website Builder](https://www.odoo.com/app/website),
[eCommerce](https://www.odoo.com/app/ecommerce),
[Warehouse Management](https://www.odoo.com/app/inventory),
[Project Management](https://www.odoo.com/app/project),
[Billing &amp; Accounting](https://www.odoo.com/app/accounting),
[Point of Sale](https://www.odoo.com/app/point-of-sale-shop),
[Human Resources](https://www.odoo.com/app/employees),
[Marketing](https://www.odoo.com/app/social-marketing),
[Manufacturing](https://www.odoo.com/app/manufacturing),
[...](https://www.odoo.com/)

Odoo Apps can be used as stand-alone applications, but they also integrate seamlessly so you get
a full-featured [Open Source ERP](https://www.odoo.com) when you install several Apps.

## Getting started with Odoo

For a standard installation please follow the [Setup instructions](https://www.odoo.com/documentation/master/administration/install/install.html)
from the documentation.

To learn the software, we recommend the [Odoo eLearning](https://www.odoo.com/slides),
or [Scale-up, the business game](https://www.odoo.com/page/scale-up-business-game).
Developers can start with [the developer tutorials](https://www.odoo.com/documentation/master/developer/howtos.html).

## Security

If you believe you have found a security issue, check our [Responsible Disclosure page](https://www.odoo.com/security-report)
for details and get in touch with us via email.
