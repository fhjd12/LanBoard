---
name: Maintenance / Refactor (1.1.x)
about: LanBoard 1.1.x 稳定性与工程化优化清单
title: "[1.1.x] "
labels: maintenance
assignees: fhjd12
---

## 🧩 优化背景
<!-- 为什么要做这个优化？是修 bug、提高稳定性、还是避免未来踩坑？ -->

## 🎯 目标
<!-- 本次 Issue 要解决的核心问题，一句话即可 -->

---

## ✅ 优化清单（逐条勾选）

### 🔴 P0（必做：影响可用性 / 会反复踩坑）
- [ ] 路径体系统一（BASE_DIR / resource_path）
- [ ] make_icon() 兼容性修复（Pillow API 差异）
- [ ] uploads 定时清理真正生效（后台线程 / 定时器）

### 🟠 P1（强烈建议：体验 / 一致性 / 防绕过）
- [ ] 上传接口强制 MAX_FILE_MB 限制（不只在 WS）
- [ ] 下载强制 Content-Disposition（iOS/Safari 兼容）
- [ ] WebSocket 握手失败可诊断（最小日志 + 前端提示）

### 🟡 P2（工程化优化：长期收益）
- [ ] 版本号单一来源（托盘 / 页面 / 打包信息一致）
- [ ] config.json 原子写（防止半截文件）
- [ ] 多网卡 / VPN 场景 IP 提示优化

---

## 🧪 验收标准（必须满足）
<!-- 勾选前请确认以下条件 -->

- [ ] `.py` 直接运行正常
- [ ] onedir / onefile exe 运行正常
- [ ] 托盘图标与 tooltip 正确
- [ ] uploads / data / config.json 路径正确（不落到 System32）
- [ ] iOS / Android / PC 均可正常访问
- [ ] 未明显增加 CPU / 内存占用

---

## 🔍 测试环境
- OS：
- Python 版本：
- 打包方式：onedir / onefile
- 是否启用 VPN：

---

## 📝 备注
<!-- 记录实现思路、踩坑点、后续可以改进的地方 -->
