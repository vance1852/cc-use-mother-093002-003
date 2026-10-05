# 控制数贸首发披露协作基础服务

本项目提供跨境数字贸易合作业务共享的服务端基础能力，负责合作机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

## 目录

- src/digital_trade_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- src/launch_disclosure/：技术首发与受限披露管理服务（成果版本快照、披露流转、会签、凭证、冻结与审计查询）；
- tests/：基础规则、事务边界、接口路由和端到端验收测试。

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

## 技术首发与受限披露管理服务

src/launch_disclosure/ 在基础服务的审计链、时钟与异常原语之上，提供面向展会首发的受限披露能力：

- 成果版本把贡献主体、证明材料、适用辖区、公开时间、专利或监管前置事项、保密承诺、受众关系和允许查看的字段范围整体哈希为证据快照，版本一经登记不可变，更正只能登记更新版本；
- 披露申请在草拟、核验、会签、限时预览、正式公开、撤回、更正之间流转，创建时必须引用版本当前的证据快照，同一版本同时只允许一条进行中的申请；
- 会签要求研发、法务、媒体联络三个角色各自批准，申请创建人不得参与批准，任何角色不能代替其他角色；
- 下载凭证受席位、期限和用途约束，受众必须满足版本的保密承诺；访问按受众关系过滤字段，权利异议或监管限制只冻结受影响的字段与受众关系，已发生的访问记录完整保留；
- 所有写接口按 request_id 幂等，重复提交返回原回执，重试下载不会产生第二条访问记录；
- recover_expired 在系统恢复后回收到期凭证并标记超期未核验的申请；access_report 回答某人在某一时刻看到了哪版资料、哪些字段、依据了哪些批准。

### 离线验收

    PYTHONPATH=src python3 -m launch_disclosure.acceptance

### HTTP 服务

    PYTHONPATH=src python3 -m launch_disclosure.api --database disclosure.sqlite3 --host 127.0.0.1 --port 8081
