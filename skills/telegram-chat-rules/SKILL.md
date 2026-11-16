# telegram-chat-rules

## Name

telegram-chat-rules

## When to use

For group-chat reply behaviour on the Telegram bridge, in any group where Quinn is replying. This governs whether to reply, what to reply to, and how to reply. It says nothing about tone or personality.

Read CHAT_RULES.md in this skill folder for the full rules.

## Short version

- Default is silence. A group reply is the exception and needs a reason.
- Reply once, in a thread, only when addressed to Quinn, the question is still open, Quinn is the one agent it fits, and the reply adds something new.
- Stay out of instructions to another bot.
- Acknowledge once, then stop. An ack is terminal.
- If a reply would only say seen, agreed, done, or working on it, react instead of sending text.
- In a trusted multi-bot group where the telegram-collab-protocol skill applies, that protocol wins over these rules.

## Available connectors

- [Telegram Muse Bot](~/workspace/custom-connectors/custom.telegram-muse-bot/CONNECTOR.md)
