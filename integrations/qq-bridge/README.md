# qq-bridge 集成补丁

上游：[Derpyu520/qq-bridge](https://github.com/Derpyu520/qq-bridge)。当前账户对该上游只有读取权限，因此不向上游推送，也不自动创建公开 fork。完整的集成变更作为 Git 补丁随本仓库备份。

- 基线提交：`a49877f4e58d04e47532d44b803360d0f001160d`
- 第一份补丁提交：`c4c30d02f65ed01ca3e18db7127b042b49a2c2f0`
- 第二份补丁 / 当前功能提交：`9d3dccf294786bdf4f1baccb9c67fceb516deb24`，调整硬件简单闲聊的回复长度指引，保留详细问题的信息和原有权限。
- 当前功能树：`2444eef29a052990126119128451a2ebf87c6360`，两份补丁在干净基线顺序回放后已核对一致。
- 许可证：见原上游许可证副本 `UPSTREAM-LICENSE.txt`。补丁不包含 SnowLuma 二进制、账号登录数据、模型、个人声线、运行配置、令牌或聊天记录。

本机 `D:\dafeiyu\qq-bridge` 已经包含该提交，不应重复应用。以下仅用于干净基线恢复或异机同步：

```powershell
git switch -c codex/hardware-chat a49877f4e58d04e47532d44b803360d0f001160d
git am <本目录中的0001补丁文件> <本目录中的0002补丁文件>
```

补丁内 `docs/HARDWARE_CHAT.md` 说明完整链路、绑定和权限。它新增的是 qq-bridge 自身管理的实体聊天渠道，不是另开 standard DSH 会话演示工具。

本地验证的独立暂存快照通过了 qq-bridge 38/38 审计脚本；可选独立 voice-tool、ffmpeg 或真实 QQ 网关缺失的段落按实际情况标记跳过，不算实时收发验收。

第二份补丁另通过 10 项硬件检查及真实 stdio MCP 检查；没有把 QQ 渠道统一改成硬件短答规则。
