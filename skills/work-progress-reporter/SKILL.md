---
name: work-progress-reporter
description: 中文个人工作进度与周报管理 skill。用于用户私聊启用工作进度管理、制定本周或下周计划、记录每日进展、临时新增任务、登记收获/阻塞/周末加班，并在周五生成中文 Markdown 周报。所有用户独立使用且互相不可见；群聊触发时只做隐私引导，不记录进度。
metadata:
  cow:
    emoji: "📊"
    requires:
      bins: ["python3"]
---

# 工作进度周报助手

## 使用边界

- 只在私聊中记录和展示个人工作进度。群聊里有人触发本技能、汇报进度或询问周报时，只回复：`为保护个人工作进度隐私，请私聊我启用或汇报工作进度。`
- 如需用脚本返回一致的群聊隐私引导文案，运行 `privacy-notice`；该命令不读取也不写入任何用户状态。
- 每个用户的数据只属于当前 `memory_user_id`。不要读取、展示、推断或合并其他用户的状态文件、提醒任务或周报。
- 所有说明、追问、周报、错误提示都使用中文。
- 本 skill 不是团队进度看板，不提供管理员查看他人进度的能力。

## 数据脚本

脚本路径：

```powershell
py -3 <base_dir>\scripts\work_progress.py <command> --workspace <agent_workspace> --memory-user-id <memory_user_id> ...
```

在 Linux/macOS 环境可把 `py -3` 换成 `python3`。

脚本只读写当前用户私有目录：

```text
<agent_workspace>/memory/users/<memory_user_id>/work-progress/state.json
<agent_workspace>/memory/users/<memory_user_id>/work-progress/reports/YYYY-Www.md
```

必须传入当前会话上下文里的 `memory_user_id`。如果缺失、包含路径分隔符或试图穿越目录，脚本会拒绝执行。

常用命令：

```powershell
py -3 <base_dir>\scripts\work_progress.py init-user --workspace <agent_workspace> --memory-user-id <memory_user_id> --actor-id <actor_id>
py -3 <base_dir>\scripts\work_progress.py set-week-plan --workspace <agent_workspace> --memory-user-id <memory_user_id> --tasks-json '[{"title":"任务A","current_percent":20,"target_percent":80}]' --weekend-days saturday,sunday
py -3 <base_dir>\scripts\work_progress.py record-checkin --workspace <agent_workspace> --memory-user-id <memory_user_id> --progress-text "昨天完成..." --learnings "收获..." --blockers "阻塞..." --new-tasks-json '[{"title":"临时任务","current_percent":0,"target_percent":30}]'
py -3 <base_dir>\scripts\work_progress.py add-task --workspace <agent_workspace> --memory-user-id <memory_user_id> --title "新增任务" --current-percent 0 --target-percent 50
py -3 <base_dir>\scripts\work_progress.py generate-report --workspace <agent_workspace> --memory-user-id <memory_user_id>
py -3 <base_dir>\scripts\work_progress.py get-status --workspace <agent_workspace> --memory-user-id <memory_user_id>
py -3 <base_dir>\scripts\work_progress.py schedule-plan --workspace <agent_workspace> --memory-user-id <memory_user_id>
```

脚本默认输出 JSON。`generate-report` 的 `report_markdown` 可以直接作为聊天周报正文发送给用户。

## 首次启用流程

当用户私聊说“启用工作进度管理”“帮我管理本周工作进度”“以后提醒我写周报”等同类意图时：

1. 读取本技能说明。
2. 运行 `init-user` 初始化当前用户状态。
3. 如果本周还没有计划，先简短介绍：
   `我可以帮你每天记录工作进度、临时任务和收获，并在周五整理成中文周报。这个功能只在私聊记录，其他人看不到。`
4. 询问用户：
   - 本周有哪些任务；
   - 每个任务当前大约完成百分比；
   - 本周五希望达到多少百分比；
   - 本周末是否加班，如果加班是周六、周日还是两天。
5. 用户回复后运行 `set-week-plan`。
6. 运行 `schedule-plan`，根据返回的 `scheduler_actions` 调用 `scheduler` 工具创建或更新提醒。

## 每日提醒文案

固定提醒建议使用 `scheduler` 的 `ai_task`，让 Agent 到点后按本 skill 生成追问，不要只发送固定消息。

周二到周五 10:00 的提醒内容：

```text
请使用 work-progress-reporter skill。当前是工作进度日报提醒。请只面向当前私聊用户，询问TA昨天的工作进度如何、当前进度百分比是否有变化、有什么收获或阻塞，并显式询问是否有新增任务。不要记录任何信息，等用户回复后再调用脚本保存。
```

周五 16:00 的提醒内容：

```text
请使用 work-progress-reporter skill。当前是周五周报提醒。请询问TA今天的工作进度、当前进度百分比、收获、阻塞和新增任务；用户回复后调用脚本记录，再生成本周中文 Markdown 周报，并询问下周计划和目标进度。
```

周一 10:00 兜底提醒内容：

```text
请使用 work-progress-reporter skill。当前是周一计划兜底提醒。若当前用户本周还没有计划，请询问本周任务、当前进度、本周目标进度和周末是否加班；若已有计划，只做简短确认，不重复打扰。
```

周末 17:00 加班提醒内容：

```text
请使用 work-progress-reporter skill。当前是周末加班进度提醒。仅当用户此前确认今天加班时，询问今天的工作内容、进度变化、收获或阻塞；记录时把内容归入下一周。
```

## 用户回复后的处理

用户回复日报或主动汇报时：

1. 判断是否来自私聊；群聊直接隐私引导并停止。
2. 从回复中提取：
   - 进度描述；
   - 任务当前百分比；
   - 收获；
   - 阻塞；
   - 新增任务。
3. 调用 `record-checkin`。如果用户只说“新增任务”，也可以调用 `add-task`。
4. 根据脚本返回的 `risk_hints` 做温和提醒，例如“这个任务当前进度低于本周目标，周报里我会标为风险项。”
5. 回复要短，确认已记录，并继续鼓励用户补充遗漏信息。

## 周五周报流程

周五 16:00 用户回复当天进度后：

1. 先用 `record-checkin` 保存当天进度。
2. 调用 `generate-report`。
3. 把返回的 `report_markdown` 直接发给用户。
4. 继续询问下周计划、下周目标进度、下周末是否预计加班。
5. 用户给出下周计划后，用 `set-week-plan --week-offset 1` 保存，并重新运行 `schedule-plan`。

周报必须包含：

- 本周工作内容；
- 进度情况；
- 新增/临时任务；
- 风险与未达项；
- 本周收获；
- 下周计划与目标进度。

## 调度创建规则

通过 `schedule-plan` 获取需要执行的动作。动作格式由脚本返回，例如：

```json
{
  "scheduler_actions": [
    {
      "op": "create",
      "name": "工作进度日报提醒",
      "schedule_type": "cron",
      "schedule_value": "0 10 * * 2-5",
      "ai_task": "..."
    }
  ]
}
```

执行规则：

- `op=create`：调用 `scheduler` 创建任务。
- `op=keep`：不做事。
- `op=delete`：如果任务 ID 存在且属于当前用户，调用 `scheduler` 删除。
- 创建成功后，把 scheduler 返回的任务 ID 用 `save-scheduler-task` 写回状态；一次性任务要同时传 `--run-at`。
- 删除成功后，用 `remove-scheduler-task --task-key <task_key>` 移除本地任务编号记录。
- 不要直接编辑 `scheduler/tasks.json`。

固定任务：

- 周二到周五 10:00：`0 10 * * 2-5`
- 周五 16:00：`0 16 * * 5`

条件任务：

- 周一 10:00：仅当没有从上周五拿到本周计划时创建一次性兜底。
- 周末 17:00：仅当用户明确对应日期加班时创建一次性提醒，周末内容归入下一周。

## 进度风险

v1 不做线性延期算法，只保存 `target_percent` 和 `current_percent` 并做差距提示：

- `current_percent < target_percent`：列为“低于本周目标”。
- `current_percent >= target_percent`：列为“达到或超过目标”。
- 缺少百分比时不要编造，提示用户下次可以补充百分比。

## 输出风格

- 日常确认简洁，避免长篇复述。
- 周报使用 Markdown，标题和条目清晰，语气专业自然。
- 不暴露文件路径、`memory_user_id`、`actor_id` 或 scheduler 任务 ID，除非用户明确要求排查。
