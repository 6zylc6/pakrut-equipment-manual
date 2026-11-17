# 帕鲁特金矿智能运维平台 V4.2｜公网部署版

本项目将现有矿区运维平台部署为可从不同地点通过浏览器访问的网站。**GitHub 用来保存代码，Railway 负责运行 Python 后端。** 不需要让你的笔记本一直开机。

## 文件说明

- `index.html`：平台网页页面。
- `server.py`：Python 后端。
- `requirements.txt`：Python 依赖。
- `Dockerfile`、`railway.json`：Railway 自动构建和部署配置。
- `本地运行说明.txt`：在 Windows 笔记本上本地运行的步骤。
- `本地启动网站.bat`、`安装运行依赖.bat`：Windows 本地运行辅助脚本。
- `库存配件导入模板.csv`：库存配件导入模板。

## 第一步：上传到 GitHub

1. 登录 GitHub，创建一个新仓库（Repository），建议名称：`palute-mine-maintenance`。
2. 仓库可见性选择 **Private（私有）**，不要选 Public。
3. 进入新仓库，选择 **Add file → Upload files**。
4. 把本文件夹中的文件和文件夹内容上传。应能看到 `index.html`、`server.py`、`requirements.txt`、`Dockerfile`、`railway.json`、`README.md` 等文件。
5. 点击 **Commit changes**。

不要上传 API Key、`.env`、`ark_config.json`、`inventory_shared.db` 等密钥或本地数据文件。`.gitignore` 已对这些文件进行排除。

## 第二步：在 Railway 部署

1. 打开 https://railway.com/ ，使用 GitHub 登录并授权访问刚才创建的私有仓库。
2. 新建项目，选择从 GitHub Repo 部署，选择 `palute-mine-maintenance`。
3. 在服务的 **Volumes** 中添加一个持久化卷，Mount Path 填：`/data`。
4. 在服务的 **Variables** 中添加以下变量：

   | 变量名 | 值 |
   |---|---|
   | `APP_ENV` | `production` |
   | `APP_USERNAME` | `admin`（可自定义） |
   | `APP_PASSWORD` | 自己设置至少 12 位的强密码 |
   | `SESSION_SECRET` | 使用下面命令生成的随机值 |
   | `INVENTORY_DB` | `/data/inventory_shared.db` |
   | `ARK_CONFIG_FILE` | `/data/ark_config.json` |

   在 Windows 命令提示符（CMD）运行以下命令，将输出填入 `SESSION_SECRET`：

   ```bat
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

   `APP_PASSWORD` 和 `SESSION_SECRET` 只填写在 Railway Variables，不要放入 GitHub 或发给别人。

5. 确认 Railway 使用仓库根目录的 `Dockerfile` 构建并部署。
6. 部署成功后，在服务的 **Settings → Networking → Public Networking** 中点击 **Generate Domain**，生成 `*.up.railway.app` 网站地址。
7. 使用新网址打开网站，输入 `APP_USERNAME` 和 `APP_PASSWORD` 登录。

## 第三步：配置 AI 助手

1. 登录公网网站，进入“AI 助手 → 接口设置”。
2. 在 API Key 输入框填写自己的火山方舟 API Key。
3. 模型 ID 填写控制台中已开通且测试成功的模型 ID。
4. 保存配置，再测试连接。

API Key 会写入 Railway 持久化卷中的 `/data/ark_config.json`，不会写入网页或 GitHub。**云端需要重新填写一次 API Key**，原笔记本上的本地配置不会自动同步。

## 第四步：迁移原有库存数据（如有）

1. 打开原笔记本上的 V4.1 网站，进入“库存与出入库”，点击“备份库存 JSON”，保存备份文件。
2. 登录新的公网网站，进入“库存与出入库”，选择刚保存的 JSON 文件并导入。
3. 系统只允许在云端库存库为空时执行完整备份恢复，以免重复导入流水。
4. 设备档案与知识库已包含在 `index.html` 中，不需要单独迁移。

## 费用和安全注意事项

- Railway 的免费额度、资源上限和付费标准可能调整。部署前请在 https://railway.com/pricing 查看账户实际可用额度和价格。
- 本平台当前使用 SQLite 单实例，不要扩展为多个服务副本。
- 定期从平台导出库存 JSON，并另存到安全位置。持久化卷不能代替备份。
- 当前采用单一共享登录账号，适合小范围授权人员试用；不是带多角色权限、操作审批和完整审计的生产级矿山信息系统。
- 不要把网站公开给所有人使用；只把登录账号密码交给获准人员。离职或不再授权时更换密码。
- 本平台是运维辅助工具，不替代矿山安全规程、设备联锁、PLC 或正式生产控制系统。
