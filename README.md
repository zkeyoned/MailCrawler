# 邮件爬虫（MailCrawler）

一个开源的桌面小工具：把学校邮箱（腾讯企业邮箱）的邮件同步到本机，用 AI（Jev / TypeSafe）把每封邮件分成七类、判断「要不要动手」、提取截止日期，再用一群「蜘蛛爬字」的可视化展示出来。支持按截止日期清理收件箱——但**只移到「已删除」文件夹，绝不永久删除**。

本软件是社区开源项目，**与任何学校无关，不是学校官方出品**。

## 功能

- **登录页**：输入学校邮箱（完整地址）和密码，以及自己的 TypeSafe API key；学号只用于自动猜测邮箱地址（可改）。
- **可视化爬取**：一封邮件对应一只蜘蛛，在字上逐词爬；顶部计数 CRAWLERS / EMAILS / WORDS READ / SENT TO JEV / EXPIRED；4 / 9 / 12 / 20 格切换。
- **七个类别**：课程作业考试、教务行政、校园生活与服务、讲座活动比赛、实习招聘、群发通知与订阅、写给你本人。
- **视图**：全部、待处理（要动手且没过截止）、已过期 · 可删、按类别浏览；搜索主题/发件人/正文；列表点行打开全文抽屉。
- **日期范围同步**：可设定从几号到几号（IMAP SINCE / BEFORE），只读。
- **新邮件自动同步**：设置里开关，默认关闭；开启后按间隔增量同步（只扫最近 7 天）。
- **移到已删除**：在「已过期 · 可删」视图勾选后批量移动，弹确认框写明封数；只移到自己的「已删除」文件夹，可随时在网页邮箱恢复。

## 安装

### 直接下载

Releases 页面下载对应系统的压缩包，解压即用（macOS 首次打开如被拦截：右键 → 打开）。

### 自己打包

需要 Python 3.10+。

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt pyinstaller pillow

./build.sh                          # macOS → dist/MailCrawler.app
build.bat                           # Windows → dist\MailCrawler\MailCrawler.exe
```

## 登录说明

1. **邮箱地址**：必须填完整地址（形如 `yourname@mail.example.edu.cn`），不能只填学号。
2. **密码**：学校邮箱密码。如果网页邮箱「设置 → 微信绑定」里开了**安全登录**，普通密码登录会失败，两种解决办法：
   - 在网页邮箱里生成**客户端专用密码**，用它登录；或
   - 在网页邮箱里把安全登录关掉。
   登录失败时界面会再次提示这段话。
3. **TypeSafe API key**：到 [console.typesafe.ai](https://console.typesafe.ai) 注册，**先充值**，然后在控制台创建 API key（形如 `apikey_...`）。分类按 token 计费（费用极低，一般几美分），界面左下角有实时花费估计。
4. 密码和 key 只保存在本机**系统钥匙串**（macOS 钥匙串 / Windows 凭据库），不写进任何文件。

> 学号自动猜测的规则来自零星样本（去掉学号首位、前面加 `v`），**未经学校确认**，只是省事的建议——填错了直接改邮箱地址即可。

## 隐私说明（重要）

- 同步邮件是**只读**的（IMAP `readonly` + `BODY.PEEK`），不会把邮件标成已读。
- 分类时，**邮件主题和正文会发送到 TypeSafe（一家美国公司）的服务器**（`api.typesafe.ai`）做 AI 分类。是否使用由你自己决定；介意的话就不要登录使用。
- 邮件正文、分类结果只保存在你本机的应用数据目录；退出登录会删除本地数据库并清除钥匙串里的密码和 key。
- 删除邮件只移动到你邮箱自己的「已删除」文件夹（优先 `UID MOVE`），**绝不永久删除、绝不清空已删除文件夹**。

## 使用入门

1. 打开 app，登录（见上）。
2. 在「同步范围」里设定日期区间（建议先从小范围试起），点「＋ 新一轮爬取」。
3. 看蜘蛛爬字：爬完后自动给出类别、要不要动手、截止日期。
4. 「待处理」是要你动手且还没过截止的邮件；「已过期 · 可删」里勾选好封后可以一键移到已删除。
5. 需要持续跟进的话，在设置里打开「新邮件自动同步」。

## 安全设计

- 本地 HTTP 服务只监听 `127.0.0.1` 随机端口，校验 Host / Origin，别的网页调不到。
- 单实例锁：同时只能开一个窗口，避免多个窗口抢同一个分类队列。
- 前端渲染邮件一律用 `textContent`，不拼 HTML，防注入。
- 同步 `select(readonly=True)` + `BODY.PEEK`；唯一写操作是「移到已删除」，且删除前有写明封数的确认框。

## 开发

```bash
python tests/test_ddl.py     # 截止日期提取的回归测试
python -m mailcrawler        # 直接以桌面窗口运行（开发模式）
```

目录结构：

```
mailcrawler/
  __main__.py    # 入口：单实例锁 + pywebview 窗口
  server.py      # 本地 HTTP 服务与全部 API
  mailbox.py     # IMAP 同步（只读）与移到已删除
  jev.py         # Jev（TypeSafe）分类
  ddl.py         # 截止日期候选提取（正则）
  store.py       # SQLite 存储
  secrets.py     # 系统钥匙串存取（keyring）
  config.py      # 非敏感配置
  paths.py       # 应用数据目录
  static/        # 前端（无构建步骤）
packaging/mailcrawler.spec   # PyInstaller 配置
scripts/make_icon.py         # 图标生成
```

## License

MIT（见 [LICENSE](LICENSE)）。
