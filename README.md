# 多端 SSH 配置台

一个单文件网页：填写服务器和 Git 账号，生成 Linux / macOS / Windows 通用的

- SSH 配置（别名、跳板机 ProxyJump、Agent 转发、连接复用）
- 一键初始化脚本 `fleet-init.sh` / `fleet-init.ps1`：生成密钥、分发公钥、下发远端配置、设置 Git 多账号、自检
- 批量脚本 `multi-git.sh` / `multi-git.ps1` + `hosts.csv`：多台机器、多个仓库并行 pull / push / status
- Windows 服务器 OpenSSH 安装脚本、快速切换函数、常见报错排查

## 使用

直接用浏览器打开 `index.html`（或开启 GitHub Pages）。网页不会连接任何服务器，也不需要填写密码；内容只保存在当前浏览器的 localStorage 里。

把生成的脚本复制保存后，在你自己的电脑上运行：

```bash
bash fleet-init.sh                                        # Linux / macOS / WSL
powershell -ExecutionPolicy Bypass -File .\fleet-init.ps1  # Windows，首次以管理员身份运行
./multi-git.sh pull                                       # 批量 pull
```
