# 贡献指南

> 适用版本：`v1.0.0` 起。**主干 `main` 已锁定**：禁止直接 push，所有变更（含 hotfix）一律走 PR。

---

## 1. 分支模型

| 分支 | 用途 | 保护 |
|---|---|---|
| `main` | 唯一主干，始终对应一个已发布 Tag | **锁定**：禁 force-push、禁删除、禁直推，必须 PR + 至少 1 次审批 + CI 全绿 |
| `feature/<stage>-<slug>` | 阶段开发（如 `feature/w6-k8s-manifests`） | 无 |
| `fix/<issue>-<slug>` | 缺陷修复 | 无 |
| `hotfix/<version>-<slug>` | 线上紧急修复（从 Tag 切出） | 无，但 PR 目标为 `main` 且需 2 次审批 |

**分支命名规则**：小写 + 连字符，禁止中文与空格；单分支存活周期 ≤ 2 周。

---

## 2. 提交信息规范

采用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，中文正文。

```
<type>(<scope>): <摘要，不超过 50 字>

<可选正文：为什么这么改 / 取舍 / 影响面>

<可选脚注：Refs #123 / BREAKING CHANGE: ...>
```

**type 取值**

| type | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 缺陷修复 |
| `refactor` | 重构（不改行为） |
| `perf` | 性能优化 |
| `test` | 测试新增或调整 |
| `docs` | 文档 |
| `build` | 构建 / 依赖 / 镜像 |
| `ci` | CI 流水线 |
| `chore` | 杂务（版本、忽略规则、清理） |
| `revert` | 回滚 |

**scope 取值**：`W1`~`W5` 阶段名、`P2`、模块名（`memory` / `mcp` / `otel` / `ci` / `deploy` / `docs` / `repo`）。

**示例**

```
feat(P2): 完成记忆生命周期+RAG字段契约
fix(otel): 节点 span 重复上报，改为活跃节点计数去重
docs(v1.0.0): 文档收拢到 docs/ 并重写根 README
chore(repo): 清理缓存与运行期产物，发布 v1.0.0
```

---

## 3. PR 流程

1. 从 `main` 切出 `feature/*` 或 `fix/*` 分支。
2. 本地跑完 §5 的校验清单，**全绿**才提 PR。
3. PR 描述固定回答四件事：
   - 改了什么（文件级）
   - 为什么改（问题/需求）
   - 怎么验证的（命令 + 关键输出）
   - 影响面与回滚方式
4. CI 五个 job 必须全绿：`quality` / `test`（4 分片）/ `coverage` / `scripts` / `docker`（仅 main、tag 触发）。
5. 至少 1 次审批（hotfix 为 2 次），使用 **Squash merge**，合并后删除源分支。
6. 合并进 `main` 后**不自动打 Tag**；发版由维护者按 §4 手动打附注 Tag。

**禁止事项**

- 禁止 `git push --force` 到 `main`（含 `--force-with-lease`）。
- 禁止跳过 CI（`--no-verify`、`[skip ci]`），CI 失败必须先修根因。
- 禁止在 PR 中夹带与描述无关的格式化噪音（`ruff format` 全仓重排需单独 PR）。

---

## 4. 版本与 Tag

- 语义化版本 `vMAJOR.MINOR.PATCH`：`MAJOR` = 阶段里程碑（如 W 阶段合并），`MINOR` = 新增能力，`PATCH` = 修复。
- Tag **必须是附注标签**（`git tag -a`），消息格式：`Release vX.Y.Z <一句话说明>`。
- Tag 推上去后不可移动、不可删除；发现问题只能发新版本。

```bash
git tag -a v1.0.1 -m "Release v1.0.1 修复 SSE 会话跨副本回收竞态"
git push origin v1.0.1
```

---

## 5. 提交前本地校验清单

```bash
pytest -q                                          # 期望 412 passed
ruff check .                                       # 期望 All checks passed!
ruff format --check app tests                      # 期望 already formatted
python scripts/verify_deploy_manifest.py            # 期望 26/26 · DEPLOY MANIFEST OK
python scripts/verify_seed.py                       # 期望 26/26 · SEED VERIFIED
python scripts/demo_three_flows.py                  # 期望 3/3 链路 · 21/21 步骤
```

新增模块需带测试，且覆盖率门槛：**新增模块 ≥85%**（全仓基线 62.10% 仅留档，见 README §7 L2）。

---

## 6. 主干分支保护配置

### 6.1 GitHub 网页配置

`Settings → Branches → Add branch protection rule`，Branch name pattern 填 `main`，勾选：

| 配置项 | 值 |
|---|---|
| Require a pull request before merging | ✅ |
| └ Require approvals | ✅ 1（hotfix 建议 2） |
| └ Dismiss stale pull request approvals when new commits are pushed | ✅ |
| └ Require review from Code Owners | 可选（需先建 `.github/CODEOWNERS`；未建时该项不生效） |
| Require status checks to pass before merging | ✅ |
| └ Require branches to be up to date before merging | ✅ |
| └ 必选检查项 | `quality` / `test (core)` / `test (e2e)` / `test (w5)` / `test (w5p2)` / `coverage` / `scripts` |
| Require conversation resolution before merging | ✅ |
| Require linear history | ✅ |
| Do not allow bypassing the above settings | ✅（管理员同样受约束） |
| Allow force pushes | ❌ |
| Allow deletions | ❌ |

### 6.2 命令行配置（等价，需 `gh` 已登录且有 admin 权限）

```bash
gh api -X PUT repos/{owner}/{repo}/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  -f "required_status_checks[strict]=true" \
  -f "required_status_checks[contexts][]=quality" \
  -f "required_status_checks[contexts][]=test (core)" \
  -f "required_status_checks[contexts][]=test (e2e)" \
  -f "required_status_checks[contexts][]=test (w5)" \
  -f "required_status_checks[contexts][]=test (w5p2)" \
  -f "required_status_checks[contexts][]=coverage" \
  -f "required_status_checks[contexts][]=scripts" \
  -F "enforce_admins=true" \
  -f "required_pull_request_reviews[required_approving_review_count]=1" \
  -F "required_pull_request_reviews[dismiss_stale_reviews]=true" \
  -F "required_pull_request_reviews[require_code_owner_reviews]=true" \
  -F "restrictions=null" \
  -F "allow_force_pushes=false" \
  -F "allow_deletions=false" \
  -F "required_conversation_resolution=true" \
  -F "required_linear_history=true"

# 校验
gh api repos/{owner}/{repo}/branches/main/protection | jq '.required_pull_request_reviews, .allow_force_pushes'
```

> `{owner}/{repo}` 替换为实际仓库（例：`dsy/agentflow-platform`）。
> 若仓库为私有免费版，`restrictions=null` 需保留；Teams 版可改为指定团队/用户白名单。

### 6.3 本地兜底钩子（可选，防止误操作）

```bash
# .git/hooks/pre-push：拦截对 main 的直接推送
cat > .git/hooks/pre-push <<'EOF'
#!/bin/sh
while read local_ref local_sha remote_ref remote_sha; do
  case "$remote_ref" in
    refs/heads/main)
      echo "✗ main 已锁定：请走 PR 合并（见 CONTRIBUTING.md）" >&2
      exit 1 ;;
  esac
done
EOF
chmod +x .git/hooks/pre-push
```

---

## 7. hotfix 流程

1. 从**出问题的 Tag** 切分支：`git switch -c hotfix/v1.0.1-sse-race v1.0.0`
2. 只改必要代码，补一条回归用例（防止二次复发）。
3. 跑完 §5 校验清单，提 PR 到 `main`，标题前缀 `[HOTFIX]`，需 2 次审批。
4. 合并后打 `v1.0.1` 附注 Tag 并推送，`CHANGELOG.md` 补 `### 修复` 段。

---

## 8. 代码约定（存量基线）

- **不修改已冻结阶段（W1~W5）的业务代码**；新增能力一律新建模块 + 开关（默认关闭，零影响）。
- 配置分模块：`w2_config.py` / `w3_config.py` / `w5_config.py` / `w6_config.py`，不改 W1 `config.py`。
- 开关默认值必须是「零影响」语义；关闭时不得产生任何执行逻辑（非「提前 return」，是**根本不调用**）。
- 降级优先：任何外部依赖（LLM / RAG / Redis / 向量库 / 采集器）失败只能降级，不能阻断主流程。
- ruff 配置**不启用 `UP` 规则**（会强制改写冻结基线），基线文件的豁免清单在 `pyproject.toml` 的 `per-file-ignores`。
