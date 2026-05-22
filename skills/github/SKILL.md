---
name: github
description: GitHub 核心操作 - 仓库管理、Fork、PR、Release、Issue 评论
metadata: {"cowagent":{"requires":{"bins":["git","curl","jq"],"env":["GITHUB_TOKEN"]},"primaryEnv":"GITHUB_TOKEN"}}
---

# GitHub Operations Skill

通过 GitHub REST API 完成核心 Git/GitHub 操作。

> API 文档: https://docs.github.com/en/rest?apiVersion=2022-11-28

---

## 通用请求格式

所有 API 调用统一携带以下 Header:

```bash
-H "Authorization: Bearer $GITHUB_TOKEN" \
-H "Accept: application/vnd.github+json" \
-H "X-GitHub-Api-Version: 2022-11-28"
```

以下示例中用 `$OWNER` 和 `$REPO` 代替具体的用户名和仓库名。

---

## 核心操作

### 1. 获取当前用户信息

```bash
curl -s https://api.github.com/user \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" | jq '{login, id, name}'
```

### 2. 创建仓库

```bash
curl -s -X POST https://api.github.com/user/repos \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -d '{"name":"$REPO","description":"desc","private":false}'
```

### 3. Fork 仓库

```bash
curl -s -X POST https://api.github.com/repos/$OWNER/$REPO/forks \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -d '{"default_branch_only":true}'
```

### 4. 推送代码

```bash
git remote set-url origin https://${GITHUB_TOKEN}@github.com/$OWNER/$REPO.git
git add . && git commit -m "message" && git push -u origin main
```

### 5. 创建 Pull Request

```bash
curl -s -X POST https://api.github.com/repos/$OWNER/$REPO/pulls \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -d '{
    "title":"PR title",
    "head":"feature-branch",
    "base":"main",
    "body":"description"
  }'
```

> 跨 Fork 提 PR 时 `head` 格式为 `your-username:branch-name`。

### 6. 评论 Issue / PR

Issue 和 PR 共用评论 API:

```bash
curl -s -X POST https://api.github.com/repos/$OWNER/$REPO/issues/$NUMBER/comments \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -d '{"body":"comment content"}'
```

### 7. 创建 Release

```bash
curl -s -X POST https://api.github.com/repos/$OWNER/$REPO/releases \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -d '{
    "tag_name":"v1.0.0",
    "name":"v1.0.0",
    "body":"release notes",
    "generate_release_notes":true
  }'
```

### 8. 创建 Issue

```bash
curl -s -X POST https://api.github.com/repos/$OWNER/$REPO/issues \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -d '{"title":"issue title","body":"description","labels":["bug"]}'
```

---

## GITHUB_TOKEN 获取方式

### 方式一: Fine-grained Personal Access Token (推荐)

1. 打开 https://github.com/settings/personal-access-tokens/new
2. 填写 Token name、Expiration
3. 选择 Repository access（建议按需选择仓库）
4. 在 Permissions 中勾选所需权限:
   - **Contents**: Read and Write（推送代码）
   - **Pull requests**: Read and Write（创建/管理 PR）
   - **Issues**: Read and Write（创建/评论 Issue）
   - **Metadata**: Read-only（自动授予）
5. 点击 Generate token，复制保存

> 快捷模板链接（推送代码 + 创建 PR）:
> https://github.com/settings/personal-access-tokens/new?name=cowagent-token&contents=write&pull_requests=write&issues=write

### 方式二: Personal Access Token (classic)

1. 打开 https://github.com/settings/tokens/new
2. 勾选 scopes: `repo`, `workflow`（如需操作 Actions）
3. 点击 Generate token，复制保存

> Classic token 权限粒度较粗，但兼容所有 API。Fork 公开仓库并提 PR 的场景建议用 classic token。

### 配置

```bash
export GITHUB_TOKEN="ghp_xxxxxxxxxxxx"
```

详细文档: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
