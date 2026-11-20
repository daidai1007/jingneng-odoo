# 部署与配置参考

## 运行结构

本机运行目录 `%LOCALAPPDATA%/OdooNativePreview`，源码 `runtime/server`，HTTP8069、PostgreSQL15432，数据库 `odoo_customer_demo`。Git是独立副本，上传不更改运行实例。

```text
OdooNativePreview/
  设置.json                    # 本机凭据，不进入Git
  odoo.conf                    # 本机配置
  pgdata/                      # 不进入Git
  odoo-data/                   # 不进入Git
  runtime/
    manage.py                  # jingneng/native/manage.py
    bootstrap.py               # jingneng/native/bootstrap.py
    server/                    # 仓库的Odoo源码树
    python/                    # 匹配安装包的Python及依赖
    pgsql/                     # PostgreSQL运行时
```

[组件来源](../native/组件来源.txt)保留版本信息。初始化脚本是历史入口，不表示现有业务案例和附件价格库已随Git迁移；现有数据库不应通过重新初始化代替升级。

## 代码与元数据

1. 在独立测试环境准备匹配分发和依赖，生成本机凭据。
2. 放置源码，保留原模块目录布局。
3. 按 `installed-modules.snapshot.json` 核对模块；定制模型字段、视图、报表和权限需验证相应模块升级。
4. 核对 `ui-metadata.snapshot.json` 和 `field-metadata.snapshot.json`。它们是配置参考；不得将本机数值ID直接用于另一数据库。带 `unresolved_model/local_id` 的关系需重新对应。
5. 核对typed参数、计划任务、表单选择及打印入口。

快照包含相关模块完整视图/动作参考，不表示每条均为新增修改。代码改动以Git基线比较和 `jingneng/audit/changed-source.json` 为准。用户、客户、订单、成本快照和图纸不在配置快照中。

### 帮助文案、权限和多语言补充

- `database-interface-supplement.snapshot.json` 补充数据库中两处帮助文案，并以稳定XML ID核对五个已同步的表单视图，不把本机数字ID当成迁移标识。
- `interface-translations.snapshot.json` 保存配置表中当前存在的原始JSONB译文，包括动作名称/帮助和视图结构的语言版本。主配置快照是单一环境语言下的读取，不能代替此多语言参考。
- 本分发使用新的 `ir.access` 统一权限模型，不存在旧版 `ir.model.access`、`ir.rule` 模型。`access-control.snapshot.json` 保存订单、订单行、图纸和产品相关的31条权限/限制配置，均已解析XML ID；不要套用旧模型名回放。
- 不导出人员及用户语言偏好。新环境按需要为本机演示或业务账号选择 `zh_CN`，角色分配由管理员在目标环境完成。

上述内容仍仅为配置参考；同步需匹配相同代码版本、目标字段和实际语言，不能将整个快照无条件写入任意数据库。

## 计价与识图依赖

- `sale.drawing_recognition.client` 为原报价客户端规则目录，计价复用 `scheme2_ui.py`、`attachment_v2_client.py` 和纯计算依赖。
- HTTP授权通过既有 `QuoteClient`；源码在 `jingneng/integration/复核正式报价来源.py`。当前Odoo仍引用原本机路径，迁移时需恢复相应目录或审阅后调整引用。
- `client_config.json` 的真实密钥另行在本机配置，仓库只给无密钥示例。
- 识图依赖 `bundle`、`dependencies`、`pure_dependencies`、`renderer`、`converter`、`workspace` 的实际组件与路径；这些二进制、模型、用户图纸和缓存不随源码上传。
- `sale.selection_accessory_catalog_path` 等目录路径需在新环境核对，价格库不以假数据替代。
- 现有正式案例及成本快照由业务库和受控导入文件提供，未上传Git。既有导入代码保留，输入数据须另行提供。

依赖或规则缺失时，应保留旧金额并提示缺项，不伪造成功结果。

## 恢复销售自动物流

当前库参数：

```python
env['ir.config_parameter'].sudo().get_bool('sale.automatic_stock_procurement', True)
# False
```

技术部门完成真实仓库/补货/库存路径后，管理员可恢复：

```python
env['ir.config_parameter'].sudo().set_bool('sale.automatic_stock_procurement', True)
env.cr.commit()
```

开关控制后续销售自动补货入口，不自动补做旧订单物流。本次未新增技术部专用启动按钮，旧订单实际物流按技术部门流程安排。

## 配置模板

`odoo.conf.example` 保留实际配置项，密码和运行目录换成占位符。`quote-client-config.example.json` 仅保留服务地址及无效密钥占位符。部署时自行填写，Odoo不会自动替换这些占位字符串。

本次Git归档没有启动新Odoo接口、换端口、升级数据库或创建真实报价/合同。
