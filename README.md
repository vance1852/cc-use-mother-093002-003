# 控制数贸首发披露协作基础服务

本项目提供跨境数字贸易合作业务共享的服务端基础能力，负责合作机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

## 目录

- src/digital_trade_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- src/launch_disclosure/：技术首发与受限披露管理服务（见下节）；
- tests/：基础规则、事务边界、接口路由和端到端验收测试。

## 技术首发与受限披露管理服务

`launch_disclosure` 在基础服务之上管理数贸会技术首发的受限披露：

- 每个成果版本把贡献主体、证明材料（证据快照哈希）、适用辖区、公开时间、专利或监管前置事项、保密承诺、受众关系和允许查看的字段范围写入同一条哈希串联的审计链；
- 披露申请在草拟、核验、会签、限时预览、正式公开、撤回、更正之间流转，核验由 reviewer 执行，会签要求研发方（researcher）、法务（legal）、媒体联络人（media）三个职责分别批准且不能相互代替；
- 开放资料签发受席位、期限和用途约束的下载凭证，凭证与访问记录都引用申请锁定的同一份证据快照；同一请求编号重发只回放回执，并发访问最多一个成功；
- 权利异议或新的监管限制出现时，法务只能冻结真正受影响的字段与受众，已经发生的访问记录保持可审计；
- 服务启动和每次调用维护接口时继续处理到期凭证回收与逾期未核验事项，审计员可查询某人某时刻看到的版本、字段和当时依据的批准。

### 离线验收

    PYTHONPATH=src python3 -m launch_disclosure.acceptance

### HTTP 服务

    PYTHONPATH=src python3 -m launch_disclosure.api --database launch.sqlite3 --host 127.0.0.1 --port 8080

首发披露接口挂在 `/launch/*` 前缀下（如 POST /launch/versions、POST /launch/applications/countersign、POST /launch/access、GET /launch/access-events），基础服务接口保持不变。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m digital_trade_foundation.acceptance

验收命令会在临时 SQLite 数据库中登记合作机构、操作者、业务节点和参考资料，核对幂等回执与审计链，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m digital_trade_foundation.api --database digital_trade.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
