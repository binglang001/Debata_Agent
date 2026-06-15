# Agent Instructions

## 测试要求

- 在声称“远端 CI 等价验证”前，必须先读取 `.github/workflows/` 中实际触发的 workflow，而不是用本地习惯命令替代。
- 本地验证必须尽量逐条复现 workflow：环境变量、依赖安装命令、pytest 参数、lint 命令都按 workflow 原文执行。
- 如果本机无法覆盖完整矩阵（例如缺少 Python 3.12、Linux runner 或系统依赖），必须明确说明已覆盖的矩阵和未覆盖的矩阵，不能笼统说“和远端一样”。
- 如果依赖安装因沙箱或网络失败，必须按原命令在可联网环境重试；不能跳过 `pip install ".[dev,gui]"` 等 CI 安装步骤直接跑已有环境。
- 修复远端 CI 失败前，优先在对应分支运行上述等价流程；修复后再按同一 workflow 命令复验。

## main 分支提交/合并原则

- `main` 是干净发布线，只放已验证的稳定版本提交；日常开发、新功能、探索性修复都先在 `develop` 或专题分支完成。
- 切入 `main` 前必须确认当前工作区干净；如果有未提交改动，先提交到正确分支、stash、或明确丢弃，不能把别的分支工作树带进 `main`。
- 在 `main` 上只允许 release/hotfix 级别提交；提交前必须明确版本号、CHANGELOG、依赖清单版本号和 tag 是否同步。
- 合并或同步到 `main` 前，必须按 `.github/workflows/` 的实际命令完成等价测试；不能只跑局部测试就发布。
- `main` 发布后必须确认 `git status --short --branch` 干净，并确认本地 `HEAD` 与 `origin/main` 一致。
- release 压缩包只放 ignored 的 `release/` 目录，文件名使用 `Debata-vX.Y.Z-alpha.zip`，不得提交进 Git。
- 如果需要重写或强推 `main`，先创建备份分支，再使用 `--force-with-lease`，并在推送后等待远端 CI 全矩阵完成。
