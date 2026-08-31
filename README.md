# 银行股每日扫描（大佬刘双估值战法）

基于「大佬刘战法一」的银行股买入点扫描脚本，每日自动提示哪些银行进入上车区间。扫描结果自动生成可视化面板，通过 GitHub Pages 在线浏览。

📊 在线面板：https://hyan1985.github.io/bank-scan/

## 估值逻辑

**核心公式**（源自文章）：
```
买入价 = min(PE估值价, 股息率估值价) + 即将派发分红
```

| 步骤 | 计算 | 说明 |
|------|------|------|
| PE 估值 | 预估自然年 EPS × 目标PE | 目标PE：优秀银行 7.67（15%年化 6.67 × 1.15），一般银行 6.67，超 10PE 不考虑 |
| 股息率估值 | 最近财年每股分红 ÷ 目标股息率 | 国有大行 5%+，优秀股份/城商行 3%+（按银行微调） |
| 取低者 | min(PE估值, 股息率估值) = 基准价 | 文章："两者取低者" |
| 加上分红 | 基准价 + 最近财年DPS | 文章："考虑到今年的分红马上要进行" |

**自然年 PE（线性预估）**：
```
预估自然年EPS = 线性回归(最近5个年度EPS, 目标自然年)
自然年PE = 现价 ÷ 预估自然年EPS
```
用最近 5 个年度 EPS 做最小二乘线性外推，得到当年自然年 EPS，再算 PE。比用静态上一年度 EPS 更贴近"全年"口径。

## 上车信号

| 信号 | 含义 |
|------|------|
| ● 射击范围 | 现价 ≤ 基准价（min双估值） |
| ○ 参考上车 | 现价 ≤ 参考上车价（基准价 + 即将分红） |
| … 等待 | 现价高于上车价，显示需再跌多少 |

## 使用

### 本地运行

```bash
cd /Users/hyan/Desktop/银行股
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env 填入 TUSHARE_TOKEN（必填）和 DEEPSEEK_API_KEY（可选）

python bank_daily_scan.py
```

可选参数：
```bash
python bank_daily_scan.py --no-ai      # 跳过 DeepSeek 解读
python bank_daily_scan.py --no-save    # 不保存文件
python bank_daily_scan.py --workers 4  # 并行拉取线程数
```

### 输出

- `output/bank_scan_YYYYMMDD.csv` — 全量明细
- `output/bank_scan_YYYYMMDD.xlsx` — 上车提示 Excel（含全量/上车提示/估值参数 3 个 sheet）
- `output/ai_interpret_YYYYMMDD.md` — DeepSeek 每日解读（配置 key 时）

### AI 每日解读

默认使用 `deepseek-v4-flash`（可用 `DEEPSEEK_MODEL` 覆盖）。该模型为推理模型，**默认开启 thinking mode**，会先把大量 token 消耗在思考过程上，导致 `max_tokens` 被耗尽、最终回答 `content` 为空（现象：面板文字停留在旧日期，日志显示 `HTTP 200` 但 `content=""`）。

脚本已做如下处理：

- 请求体显式传入 `"thinking": {"type": "disabled"}` 关闭推理模式，让模型直接把回答写入 `content`；
- `max_tokens` 设为 2000；
- 解析时兜底：若 `content` 为空，回退读取 `reasoning_content`。

## 定时自动跑（GitHub Actions）

工作日（周一至周五）自动扫描，扫描结果提交回仓库并更新面板：

| 时间（北京时间） | 说明 |
|------|------|
| **21:05** | 触发（UTC `5 13`） |
| **~23:00** | 实际执行（GitHub 调度器有约 2 小时延迟） |

与其他项目错峰执行，避开 GitHub 整点高负载（`00`/`30` 整点延迟明显）。

### 部署步骤

1. **在 GitHub 创建仓库**（如 `bank-scan`），`git init` 后把代码推上去：

   ```bash
   cd /Users/hyan/Desktop/银行股
   git init
   git add .
   git commit -m "银行股每日扫描"
   git branch -M main
   git remote add origin git@github.com:你的用户名/bank-scan.git
   git push -u origin main
   ```

2. **配置 GitHub Secrets**（仓库 → Settings → Secrets and variables → Actions → New repository secret）：
   - `TUSHARE_TOKEN`：你的 tushare token（必填）
   - `DEEPSEEK`：DeepSeek key（可选，填了自动生成 AI 解读）

3. Actions 会自动按计划跑。也可在 Actions 页面手动触发（Run workflow）。

### 可视化面板（GitHub Pages）

- 面板模板为 `dashboard.html`（含数据占位符），扫描后由脚本注入最新数据生成 `index.html`。
- 仓库根目录的 `index.html` 即为 GitHub Pages 入口，无需额外配置构建流程。
- 打开仓库 **Settings → Pages**，Source 选择 **Deploy from a branch**，Branch 选 `main` + `/ (root)` 即可。
- Pages 部署由 GitHub 服务端完成，若出现 `deployment_queued` / 超时，多为 GitHub 服务拥堵，数据已在仓库中，服务恢复后自动部署。

### 注意

- `.env` 已被 `.gitignore` 排除，**不要提交真实 token**。
- 扫描结果自动 commit 回仓库，需给 workflow 开启 `contents: write` 权限（已在 yml 中声明）。
- 如仓库为私有，扫描结果仅自己可见；如公开，注意结果中的投资信息会公开。

## 自定义估值参数

编辑 `bank_valuation_config.csv`，按银行微调目标：

| 字段 | 含义 |
|------|------|
| `ts_code` | 6位代码（如 `600036`） |
| `name` | 银行名 |
| `type` | 国有大行/股份行/城商行/农商行 |
| `target_pe` | 目标 PE（优秀 7.67，一般 6.67） |
| `target_div_yield` | 目标股息率 %（大行 5.5，股份/城商 3~5） |
| `quality` | 质量评级（优秀/一般） |
